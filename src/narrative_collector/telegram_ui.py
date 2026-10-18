"""Small, private Telegram review interface for X-originated idea cards."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .diagnostics import DiagnosticConfig, snapshot
from .watchlist import MAX_ACCOUNTS, MAX_FILE_BYTES, add_accounts, list_accounts, parse_txt, remove_account

LOG = logging.getLogger(__name__)
POST_ID = re.compile(r"\d{1,24}\Z")
ACTIONS = {"work": "👍 В работу", "skip": "👎 Мимо", "save": "🔖 Сохранить"}
CHAT_ID = re.compile(r"-?\d{1,20}\Z")


def parse_chat_ids(raw: str) -> tuple[str, ...]:
    ids = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not ids or len(ids) > 10 or any(not CHAT_ID.fullmatch(value) for value in ids):
        raise ValueError("TELEGRAM_CHAT_IDS must contain 1..10 numeric chat IDs separated by commas")
    return ids


def menu_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "🗂 Последние", "callback_data": "menu:latest"},
         {"text": "🔖 Сохранённые", "callback_data": "menu:saved"}],
        [{"text": "🔎 Не прошли ИИ", "callback_data": "menu:rejected"}],
        [{"text": "👀 X-аккаунты", "callback_data": "menu:watchlist"},
         {"text": "📥 Загрузить TXT", "callback_data": "menu:upload"}],
        [{"text": "📊 Статистика", "callback_data": "menu:stats"},
         {"text": "🩺 Диагностика", "callback_data": "menu:diagnostics"}],
    ]}


def telegram_request(token: str, method: str, payload: dict) -> object:
    if not token or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Invalid TELEGRAM_BOT_TOKEN")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30 if method == "getUpdates" else 25) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram {method} HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError(f"Telegram {method} network error") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise RuntimeError(f"Telegram {method} rejected request")
    return data.get("result")


def card_keyboard(post_id: str, url: str, selected: str = "") -> dict:
    if not POST_ID.fullmatch(post_id) or not url.startswith("https://x.com/"):
        raise ValueError("Invalid X card source")
    rows = [[{"text": "🔗 Открыть X-пост", "url": url}]]
    rows.append([{"text": ("✅ " if selected == action else "") + label,
                  "callback_data": f"vote:{action}:{post_id}"}
                 for action, label in ACTIONS.items()])
    return {"inline_keyboard": rows}


def send_card(token: str, chat_id: str, message: str, post_id: str, url: str) -> object:
    return telegram_request(token, "sendMessage", {
        "chat_id": chat_id, "text": message[:4000],
        "link_preview_options": {"is_disabled": True},
        "reply_markup": card_keyboard(post_id, url),
    })


def download_telegram_file(token: str, path: str) -> bytes:
    if (not path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))
            or "?" in path or "#" in path or "\\" in path):
        raise ValueError("Telegram вернул некорректный путь к файлу")
    url = f"https://api.telegram.org/file/bot{token}/{urllib.parse.quote(path, safe='/')}"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            data = response.read(MAX_FILE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError):
        raise RuntimeError("Не удалось скачать TXT из Telegram") from None
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("TXT-файл больше 64 КБ")
    return data


def _watchlist_text(db: sqlite3.Connection, page: int = 1) -> str:
    handles = list_accounts(db)
    page_size = 50
    pages = max(1, (len(handles) + page_size - 1) // page_size)
    if not 1 <= page <= pages:
        return f"Страницы 1–{pages}. Используйте /watchlist 1."
    selected = handles[(page - 1) * page_size:page * page_size]
    lines = [f"👀 Общий список X-аккаунтов ({len(handles)}/{MAX_ACCOUNTS}) · стр. {page}/{pages}",
             "\n".join("@" + item for item in selected) if selected else "Пока пусто."]
    if page < pages:
        lines.append(f"Следующая страница: /watchlist {page + 1}")
    lines.append("Чтобы убрать аккаунт: /unwatch @handle")
    if len(handles) > 60:
        lines.append("⚠️ Сейчас проверяется только 5 аккаунтов за 10 минут; "
                     "при таком списке часть постов будет пропущена. Лимит X-кредитов сохраняется.")
    return "\n\n".join(lines)


UPLOAD_HELP = ("Отправьте этому боту TXT-файл как документ: один @handle или "
               "https://x.com/handle на строку. До 1000 аккаунтов в общем списке, "
               "файл до 64 КБ. Вы и друг редактируете один общий список. "
               "Сейчас проверяется 5 аккаунтов за 10 минут, поэтому большой список "
               "не означает полный мониторинг всех постов. X-прогон и кредитный лимит должны быть активны.")


def _state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
          alert_key TEXT PRIMARY KEY, day TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS telegram_cursor (
          id INTEGER PRIMARY KEY CHECK(id=1), next_update_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS alert_deliveries (
          alert_key TEXT NOT NULL, chat_id TEXT NOT NULL, status TEXT NOT NULL,
          updated_at TEXT NOT NULL, PRIMARY KEY(alert_key,chat_id));
        CREATE TABLE IF NOT EXISTS telegram_feedback_votes (
          post_id TEXT NOT NULL, chat_id TEXT NOT NULL, reviewer_id TEXT NOT NULL,
          action TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(post_id,reviewer_id));
        CREATE TABLE IF NOT EXISTS diagnostic_notices (
          code TEXT NOT NULL, chat_id TEXT NOT NULL, last_sent_at TEXT NOT NULL,
          PRIMARY KEY(code,chat_id));
        CREATE TABLE IF NOT EXISTS rejected_view_state (
          id INTEGER PRIMARY KEY CHECK(id=1), cleared_through_id INTEGER NOT NULL);
    """)
    return db


def _allow(chat_id: str, user_id: str, configured: set[str]) -> bool:
    if chat_id.startswith("-"):
        return user_id in configured
    return chat_id == user_id or user_id in configured


def _recent(db: sqlite3.Connection, saved_only: bool, chat_id: str) -> str:
    join = ("JOIN telegram_feedback_votes f ON "
            "json_extract(a.payload_json,'$.source_post_ids[0]')=f.post_id "
            "AND f.chat_id=? AND f.action='save'") if saved_only else ""
    try:
        rows = db.execute(
            "SELECT a.payload_json,a.created_at FROM alerts a " + join +
            " JOIN alert_deliveries d ON d.alert_key=a.alert_key AND d.chat_id=? "
            "AND d.status='sent' WHERE a.status IN ('sent','partial') "
            "ORDER BY a.created_at DESC LIMIT 5",
            (chat_id, chat_id) if saved_only else (chat_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return "Карточек пока нет."
        raise
    if not rows:
        return "Сохранённых карточек пока нет." if saved_only else "Карточек пока нет."
    lines = ["🔖 Сохранённые идеи:" if saved_only else "🗂 Последние карточки:"]
    for raw, created in rows:
        data = json.loads(raw)
        model = data.get("model") or {}
        name = str(model.get("coin_name") or model.get("narrative") or "Идея")[:60]
        url = data.get("post_url") or ""
        post_ids = data.get("source_post_ids") or []
        command = f"/card {post_ids[0]}" if post_ids and POST_ID.fullmatch(str(post_ids[0])) else ""
        lines.append(f"• {name} · {created[:16]}\n{command}\n{url}")
    return "\n".join(lines)[:4000]


def _rejected_text(db: sqlite3.Connection, collector_path: Path | None,
                   page: int = 1) -> str:
    """Read bounded, previously paid-for AI decisions; never re-run the model."""
    if collector_path is None:
        return "Список отказов недоступен: путь к базе X не настроен."
    try:
        cleared = db.execute("SELECT cleared_through_id FROM rejected_view_state WHERE id=1").fetchone()
        cleared_through = int(cleared[0]) if cleared else 0
        rows = db.execute(
            "SELECT analyzed_at,post_ids_json,proposals_json FROM ai_batches "
            "WHERE id>? ORDER BY id DESC LIMIT 250", (cleared_through,)
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return "ИИ ещё не разобрал ни одного поста."
        LOG.warning("Could not read AI analysis history: %s", type(exc).__name__)
        return "История разборов ИИ сейчас недоступна. Попробуйте /rejected позже."
    rejected = []
    for analyzed_at, ids_json, proposals_json in rows:
        try:
            post_ids = json.loads(ids_json)
            proposals = json.loads(proposals_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(post_ids, list) or not isinstance(proposals, list):
            continue
        for post_id, proposal in zip(post_ids, proposals):
            if (not isinstance(post_id, str) or not POST_ID.fullmatch(post_id)
                    or not isinstance(proposal, dict)):
                continue
            if proposal.get("interesting") is True and proposal.get("confidence") == "high":
                continue
            rejected.append((str(analyzed_at), post_id, proposal))
    if not rejected:
        return ("Среди последних 250 разборов ИИ нет отклонённых постов. "
                "Посты, отсечённые до вызова ИИ, сюда не входят.")
    start = (page - 1) * 5
    selection = rejected[start:start + 5]
    if not selection:
        return f"Страница {page} пуста. Попробуйте /rejected 1."
    lines = [f"🔎 Не прошли ИИ · стр. {page}\n"
             "Это не все посты X: показаны лишь уже разобранные ИИ. "
             "Вердикт можно проверить вручную по ссылке."]
    try:
        with closing(sqlite3.connect(f"{collector_path.resolve().as_uri()}?mode=ro",
                                     uri=True, timeout=3)) as collector:
            for number, (analyzed_at, post_id, proposal) in enumerate(selection, start + 1):
                row = collector.execute(
                    "SELECT author_username,text,raw_json FROM x_first_posts "
                    "WHERE post_id=?", (post_id,)
                ).fetchone()
                author = row[0] if row and isinstance(row[0], str) and re.fullmatch(
                    r"[A-Za-z0-9_]{1,15}", row[0]) else "i"
                post_text = " ".join(str(row[1] or "").split())[:240] if row else "Пост не найден в локальной базе"
                try:
                    raw = json.loads(row[2] or "{}") if row else {}
                except (TypeError, ValueError):
                    raw = {}
                raw = raw if isinstance(raw, dict) else {}
                try:
                    views = max(0, int(raw.get("viewCount") or 0))
                    likes = max(0, int(raw.get("likeCount") or 0))
                except (TypeError, ValueError):
                    views = likes = 0
                confidence_value = proposal.get("confidence")
                confidence = {"high": "высокая", "medium": "средняя", "low": "низкая"}.get(
                    confidence_value if isinstance(confidence_value, str) else "", "неизвестна")
                verdict = ("ИИ отклонил" if proposal.get("interesting") is not True else
                           "ИИ увидел идею, но уверенность недостаточна")
                reason = " ".join(str(proposal.get("reason") or "").split())[:200]
                if not reason:
                    reason = "Для старого разбора причина не сохранена"
                url = f"https://x.com/{author}/status/{post_id}"
                lines.append(
                    f"\n{number}. @{author} · {analyzed_at[:16]} UTC\n"
                    f"Пост: {post_text}\n"
                    f"При сборе: {views:,} просмотров · {likes:,} лайков\n"
                    f"{verdict} · уверенность: {confidence}\n"
                    f"Причина ИИ: {reason}\n{url}"
                )
    except (OSError, sqlite3.Error) as exc:
        LOG.warning("Could not read X posts for rejected list: %s", type(exc).__name__)
        return "Локальная база X сейчас недоступна. Попробуйте /rejected позже."
    if start + 5 < len(rejected):
        lines.append(f"\nДальше: /rejected {page + 1}")
    return "\n".join(lines)[:4000]


def _stats(db: sqlite3.Connection, chat_id: str) -> str:
    def count(query: str) -> int:
        try:
            return int(db.execute(query, (chat_id,)).fetchone()[0])
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return 0
            raise
    cards = count("SELECT COUNT(*) FROM alert_deliveries WHERE chat_id=? AND status='sent'")
    work = count("SELECT COUNT(*) FROM telegram_feedback_votes WHERE chat_id=? AND action='work'")
    saved = count("SELECT COUNT(*) FROM telegram_feedback_votes WHERE chat_id=? AND action='save'")
    skipped = count("SELECT COUNT(*) FROM telegram_feedback_votes WHERE chat_id=? AND action='skip'")
    pending = count("SELECT COUNT(*) FROM alert_deliveries WHERE chat_id=? AND status='uncertain'")
    return (f"📊 Бот: карточек {cards}, в работе {work}, сохранено {saved}, "
            f"мимо {skipped}, ожидают сверки {pending}.")


def _diagnostics_text(diagnostics: Callable[[], tuple[str, tuple[str, ...]]] | None) -> str:
    if diagnostics is None:
        return "Диагностика доступна после установки обновления на сервер."
    try:
        return diagnostics()[0]
    except Exception:
        LOG.exception("Diagnostic snapshot failed")
        return "Диагностика сейчас недоступна. Проверьте журнал meme-idea-telegram.service."


def _answer_callback(token: str, callback: dict, api: Callable[[str, str, dict], object],
                     message: str = "") -> None:
    """An expired Telegram callback must never block the update cursor."""
    callback_id = callback.get("id")
    if not callback_id:
        return
    payload = {"callback_query_id": callback_id}
    if message:
        payload["text"] = message
    try:
        api(token, "answerCallbackQuery", payload)
    except (OSError, RuntimeError, ValueError):
        LOG.warning("Could not acknowledge Telegram callback (expired or invalid)")


def _notify_problems(db: sqlite3.Connection, token: str, chat_ids: set[str],
                     diagnostics: Callable[[], tuple[str, tuple[str, ...]]] | None,
                     api: Callable[[str, str, dict], object]) -> None:
    """Send each actionable fault at most once per hour to each allowed chat."""
    if diagnostics is None:
        return
    try:
        _, problems = diagnostics()
    except Exception:
        LOG.exception("Automatic diagnostic probe failed")
        return
    if not problems:
        return
    now = datetime.now(timezone.utc)
    labels = {
        "meme-x-first.timer": "таймер поиска X выключен",
        "meme-x-first.service": "задание поиска X завершилось с ошибкой",
        "meme-idea-analyzer.timer": "таймер ИИ выключен",
        "meme-idea-analyzer.service": "ИИ-анализатор завершился с ошибкой",
        "meme-idea-telegram.service": "бот Telegram не работает",
        "x_search_stale": "X-поиск давно не обновлялся",
        "x_search_missing": "X-поиск не стартовал",
        "collector_db": "база коллектора недоступна",
        "idea_state_db": "база бота недоступна",
    }
    reasons = list(dict.fromkeys(labels.get(code.split(":", 1)[0], "неизвестный сбой")
                                 for code in problems))
    summary = "; ".join(reasons[:3])
    code = "pipeline"
    for chat_id in chat_ids:
        row = db.execute("SELECT last_sent_at FROM diagnostic_notices WHERE code=? AND chat_id=?",
                         (code, chat_id)).fetchone()
        if row:
            try:
                last = datetime.fromisoformat(row[0])
            except ValueError:
                last = None
            if last and last.tzinfo and (now - last).total_seconds() < 3600:
                continue
        try:
            api(token, "sendMessage", {
                "chat_id": chat_id,
                "text": f"⚠️ Проблема в сборе мем-идей: {summary}. "
                        "Откройте 🩺 Диагностика или отправьте /diagnostics. "
                        "Уведомления ограничены одним в час.",
                "reply_markup": menu_keyboard(),
            })
        except (OSError, RuntimeError, ValueError):
            LOG.exception("Could not send diagnostic notice to chat %s", chat_id)
            continue
        with db:
            db.execute("INSERT INTO diagnostic_notices VALUES (?,?,?) "
                       "ON CONFLICT(code,chat_id) DO UPDATE SET last_sent_at=excluded.last_sent_at",
                       (code, chat_id, now.isoformat()))


def _handle_update(db: sqlite3.Connection, update: dict, token: str, chat_ids: set[str],
                   allowed: set[str], api: Callable[[str, str, dict], object],
                   downloader: Callable[[str, str], bytes],
                   diagnostics: Callable[[], tuple[str, tuple[str, ...]]] | None = None,
                   collector_db_path: Path | None = None) -> bool:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        origin = str((message.get("chat") or {}).get("id", ""))
        user = str((callback.get("from") or {}).get("id", ""))
        if origin not in chat_ids or not _allow(origin, user, allowed):
            _answer_callback(token, callback, api, "Нет доступа")
            return False
        callback_data = str(callback.get("data") or "")
        if callback_data in {"menu:latest", "menu:saved", "menu:rejected", "menu:stats", "menu:watchlist",
                             "menu:upload", "menu:diagnostics"}:
            # Telegram callback IDs expire quickly. A diagnostic read can take
            # several seconds on a small, busy VPS, so acknowledge first.
            _answer_callback(token, callback, api)
            section = callback_data.split(":", 1)[1]
            if section == "stats":
                response = _stats(db, origin)
            elif section == "diagnostics":
                response = _diagnostics_text(diagnostics)
            elif section == "rejected":
                response = _rejected_text(db, collector_db_path)
            elif section == "watchlist":
                response = _watchlist_text(db)
            elif section == "upload":
                response = UPLOAD_HELP
            else:
                response = _recent(db, section == "saved", origin)
            api(token, "sendMessage", {"chat_id": origin, "text": response,
                                       "link_preview_options": {"is_disabled": True},
                                       "reply_markup": menu_keyboard()})
            return True
        parts = callback_data.split(":")
        if len(parts) != 3 or parts[0] != "vote" or parts[1] not in ACTIONS or not POST_ID.fullmatch(parts[2]):
            return False
        action, post_id = parts[1:]
        row = db.execute("SELECT a.payload_json FROM alerts a JOIN alert_deliveries d "
                         "ON d.alert_key=a.alert_key AND d.chat_id=? AND d.status='sent' "
                         "WHERE a.status IN ('sent','partial') AND "
                         "json_extract(a.payload_json,'$.source_post_ids[0]')=? LIMIT 1",
                         (origin, post_id)).fetchone()
        if row is None:
            _answer_callback(token, callback, api, "Карточка не найдена")
            return False
        with db:
            db.execute("INSERT INTO telegram_feedback_votes VALUES (?,?,?,?,?) "
                       "ON CONFLICT(post_id,reviewer_id) "
                       "DO UPDATE SET action=excluded.action,chat_id=excluded.chat_id,"
                       "updated_at=excluded.updated_at",
                       (post_id, origin, user, action, datetime.now(timezone.utc).isoformat()))
        _answer_callback(token, callback, api, f"Отмечено: {ACTIONS[action]}")
        try:
            api(token, "editMessageReplyMarkup", {
                "chat_id": origin, "message_id": message["message_id"],
                "reply_markup": card_keyboard(post_id, json.loads(row[0])["post_url"], action),
            })
        except (KeyError, RuntimeError, ValueError):
            LOG.warning("Could not refresh keyboard for post %s", post_id)
        return True
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    origin = str((message.get("chat") or {}).get("id", ""))
    user = str((message.get("from") or {}).get("id", ""))
    if origin not in chat_ids or not _allow(origin, user, allowed):
        return False
    document = message.get("document")
    if isinstance(document, dict):
        try:
            if not str(document.get("file_name") or "").lower().endswith(".txt"):
                raise ValueError("Пришлите файл с расширением .txt")
            if int(document.get("file_size") or 0) > MAX_FILE_BYTES:
                raise ValueError("TXT-файл больше 64 КБ")
            file_id = document.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise ValueError("Telegram не передал ID файла")
            info = api(token, "getFile", {"file_id": file_id})
            if not isinstance(info, dict) or not isinstance(info.get("file_path"), str):
                raise RuntimeError("Telegram не вернул путь к TXT-файлу")
            handles = parse_txt(downloader(token, info["file_path"]))
            added, total = add_accounts(db, handles, user)
            response = f"Готово: добавлено {added}, всего {total}.\n\n" + _watchlist_text(db)
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
            response = f"Список не добавлен: {exc}"
        api(token, "sendMessage", {"chat_id": origin, "text": response[:4000],
                                   "reply_markup": menu_keyboard()})
        return True
    command = str(message.get("text") or "").split(maxsplit=1)[0].split("@")[0].lower()
    if command in {"/start", "/help", "/menu"}:
        response = ("Бот ищет новые X-посты, ИИ делает черновик мем-идеи, а вы оцениваете её.\n"
                    "/latest — последние карточки\n/card ID — открыть карточку\n"
                    "/saved — сохранённые\n/stats — статистика\n"
                    "/rejected [страница] — посты, которые ИИ не пропустил\n"
                    "/clear_rejected confirm — скрыть старые отказы (история останется)\n"
                    "/diagnostics — почему нет карточек\n"
                    "/watchlist [страница] — общий список X-аккаунтов\n"
                    "/upload — как загрузить TXT\n/unwatch @handle — удалить аккаунт\n"
                    "Кнопки под карточкой: в работу, мимо, сохранить. Сделок бот не совершает.")
    elif command == "/latest":
        response = _recent(db, False, origin)
    elif command == "/saved":
        response = _recent(db, True, origin)
    elif command == "/rejected":
        parts = str(message.get("text") or "").split(maxsplit=1)
        if len(parts) == 1:
            page = 1
        elif re.fullmatch(r"[1-9]|10", parts[1]):
            page = int(parts[1])
        else:
            page = 0
        response = (_rejected_text(db, collector_db_path, page) if page else
                    "Используйте /rejected или /rejected 2 (страницы 1–10).")
    elif command == "/clear_rejected":
        parts = str(message.get("text") or "").split(maxsplit=1)
        if len(parts) != 2 or parts[1].strip().lower() != "confirm":
            response = ("Чтобы скрыть текущие отказы для вас обоих, отправьте "
                        "/clear_rejected confirm. История ИИ и диагностика сохранятся; "
                        "повторного анализа это не запустит.")
        else:
            with db:
                through = int(db.execute("SELECT COALESCE(MAX(id),0) FROM ai_batches").fetchone()[0])
                db.execute("INSERT INTO rejected_view_state VALUES (1,?) "
                           "ON CONFLICT(id) DO UPDATE SET cleared_through_id=excluded.cleared_through_id",
                           (through,))
            response = ("Старые отказы скрыты из /rejected для вас обоих. "
                        "История сохранена; новые решения ИИ появятся в списке. "
                        "Повторного анализа старых постов не будет.")
    elif command == "/stats":
        response = _stats(db, origin)
    elif command in {"/diagnostics", "/diag"}:
        response = _diagnostics_text(diagnostics)
    elif command == "/watchlist":
        parts = str(message.get("text") or "").split(maxsplit=1)
        if len(parts) == 1:
            page = 1
        elif re.fullmatch(r"[1-9][0-9]?", parts[1]) and int(parts[1]) <= 20:
            page = int(parts[1])
        else:
            page = 0
        response = (_watchlist_text(db, page) if page else
                    "Используйте /watchlist или /watchlist 2 (страницы 1–20).")
    elif command == "/upload":
        response = UPLOAD_HELP
    elif command == "/unwatch":
        parts = str(message.get("text") or "").split(maxsplit=1)
        if len(parts) != 2:
            response = "Используйте /unwatch @handle."
        else:
            try:
                removed = remove_account(db, parts[1])
                response = ("Удалён из общего списка.\n" if removed else "Аккаунта не было в списке.\n") + _watchlist_text(db)
            except ValueError as exc:
                response = str(exc)
    elif command == "/card":
        parts = str(message.get("text") or "").split(maxsplit=1)
        post_id = parts[1].strip() if len(parts) == 2 else ""
        if not POST_ID.fullmatch(post_id):
            response = "Используйте /card ID из списка /latest."
        else:
            row = db.execute("SELECT a.payload_json FROM alerts a JOIN alert_deliveries d "
                             "ON d.alert_key=a.alert_key AND d.chat_id=? AND d.status='sent' "
                             "WHERE a.status IN ('sent','partial') AND "
                             "json_extract(a.payload_json,'$.source_post_ids[0]')=? LIMIT 1",
                             (origin, post_id)).fetchone()
            if row:
                data = json.loads(row[0])
                response = data.get("card_text") or "Полный текст старой карточки не сохранён."
                if data.get("post_url"):
                    api(token, "sendMessage", {
                        "chat_id": origin, "text": response,
                        "link_preview_options": {"is_disabled": True},
                        "reply_markup": card_keyboard(post_id, data["post_url"]),
                    })
                    return True
            else:
                response = "Карточка не найдена."
    else:
        return False
    api(token, "sendMessage", {"chat_id": origin, "text": response,
                               "link_preview_options": {"is_disabled": True},
                               "reply_markup": menu_keyboard()})
    return True


def poll_once(state_path: Path, token: str, chat_ids_raw: str, allowed_user_ids: set[str],
              api: Callable[[str, str, dict], object] = telegram_request,
              downloader: Callable[[str, str], bytes] = download_telegram_file,
              diagnostics: Callable[[], tuple[str, tuple[str, ...]]] | None = None,
              collector_db_path: Path | None = None, poll_timeout: int = 0,
              notify_diagnostics: bool = True) -> dict[str, int]:
    if not token:
        raise ValueError("Telegram token is required")
    if not 0 <= poll_timeout <= 20:
        raise ValueError("Telegram poll timeout must be 0..20 seconds")
    chat_ids = set(parse_chat_ids(chat_ids_raw))
    if any(chat_id.startswith("-") for chat_id in chat_ids) and not allowed_user_ids:
        raise ValueError("Group chat requires TELEGRAM_ALLOWED_USER_IDS")
    with closing(_state(state_path)) as db:
        cached_diagnostic: tuple[str, tuple[str, ...]] | None = None
        cached_diagnostic_error: Exception | None = None

        def read_diagnostic() -> tuple[str, tuple[str, ...]]:
            nonlocal cached_diagnostic, cached_diagnostic_error
            if diagnostics is None:
                return "Диагностика недоступна.", ()
            if cached_diagnostic_error is not None:
                raise cached_diagnostic_error
            if cached_diagnostic is None:
                try:
                    cached_diagnostic = diagnostics()
                except Exception as exc:
                    cached_diagnostic_error = exc
                    raise
            return cached_diagnostic

        row = db.execute("SELECT next_update_id FROM telegram_cursor WHERE id=1").fetchone()
        offset = row[0] if row else 0
        updates = api(token, "getUpdates", {"offset": offset, "limit": 50, "timeout": poll_timeout,
                                            "allowed_updates": ["message", "callback_query"]})
        if not isinstance(updates, list):
            raise RuntimeError("Telegram getUpdates returned unexpected data")
        handled = 0
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                continue
            handled += int(_handle_update(db, update, token, chat_ids, allowed_user_ids, api,
                                          downloader, read_diagnostic if diagnostics else None,
                                          collector_db_path))
            with db:
                db.execute("INSERT INTO telegram_cursor VALUES (1,?) ON CONFLICT(id) "
                           "DO UPDATE SET next_update_id=excluded.next_update_id",
                           (update["update_id"] + 1,))
        if notify_diagnostics:
            _notify_problems(db, token, chat_ids, read_diagnostic if diagnostics else None, api)
        return {"updates": len(updates), "handled": handled}


def main() -> None:
    import argparse
    import os
    import time

    from .config import load_dotenv

    parser = argparse.ArgumentParser(description="Handle Telegram idea-card commands and review buttons")
    parser.add_argument("--env-dir", default=".")
    parser.add_argument("--continuous", action="store_true", help="Use Telegram long polling")
    args = parser.parse_args()
    root = Path(args.env_dir).resolve()
    load_dotenv(root / ".env")
    ids = {value.strip() for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
           if value.strip()}
    collector_db = Path(os.getenv("DATABASE_PATH", "./data/collector.sqlite3"))
    if not collector_db.is_absolute():
        collector_db = root / collector_db
    state_path = root / "data/idea-bot-state.sqlite3"
    diagnostic_config = DiagnosticConfig(
        collector_db=collector_db,
        state_db=state_path,
        run_id=os.getenv("X_FIRST_RUN_ID", ""),
        end_at=os.getenv("X_FIRST_END_AT", ""),
        x_cap=int(os.getenv("X_FIRST_CREDIT_CAP", "0")),
        ai_daily_cap=int(os.getenv("AI_MAX_CALLS_PER_DAY", "0")),
        alert_daily_cap=int(os.getenv("AI_MAX_ALERTS_PER_DAY", "0")),
        weekly_x_cap=int(os.getenv("SOCIAL_WEEKLY_CREDIT_CAP", "0")),
    )
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_ids = os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", "")
        last_diagnostic_notice = -60.0
        while True:
            now = time.monotonic()
            notify_diagnostics = not args.continuous or now - last_diagnostic_notice >= 60
            result = poll_once(state_path, token, chat_ids, ids,
                               diagnostics=lambda: snapshot(diagnostic_config),
                               collector_db_path=collector_db,
                               poll_timeout=20 if args.continuous else 0,
                               notify_diagnostics=notify_diagnostics)
            if notify_diagnostics:
                last_diagnostic_notice = time.monotonic()
            if result["updates"] or not args.continuous:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if not args.continuous:
                break
            if not result["updates"] and time.monotonic() - now < 1:
                time.sleep(1)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Telegram interface stopped: {exc}\n")


if __name__ == "__main__":
    main()
