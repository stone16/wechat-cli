"""
将 wechat-cli export 的 markdown 里 [语音] 行替换为转录文字。

用法:
    python scripts/transcribe_export.py \\
        --chat "请给我六位数" \\
        --start-time 2026-04-07 --end-time 2026-04-14 \\
        --input  /path/to/original.md \\
        --output /path/to/transcribed.md

依赖: pilk, dashscope, ffmpeg (命令行); DASHSCOPE_API_KEY 通过 .env 注入。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wechat_cli.core.context import AppContext  # noqa: E402
from wechat_cli.core.contacts import resolve_username, get_contact_names  # noqa: E402


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def parse_time_bound(value: str, *, is_end: bool) -> int:
    dt = datetime.strptime(value, "%Y-%m-%d")
    if is_end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def find_group_msg_table(ctx: AppContext, username: str) -> tuple[str, str]:
    """Return (decrypted_msg_db_path, Msg_<md5> table name) for the target chat."""
    table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
    for rel_key in ctx.msg_db_keys:
        db_path = ctx.cache.get(rel_key)
        if not db_path:
            continue
        with closing(sqlite3.connect(db_path)) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists:
                return db_path, table
    raise RuntimeError(f"聊天 {username} 的 Msg_ 表未在任何已知 message shard 中找到")


def find_media_db(ctx: AppContext) -> str:
    for k in ctx.all_keys:
        rel = k.get("rel_path") if isinstance(k, dict) else k
        if rel and "media_0" in str(rel):
            path = ctx.cache.get(rel)
            if path:
                return path
    raise RuntimeError("media_0.db 密钥缺失（init --force 重跑）")


def lookup_chat_name_id(media_db_path: str, username: str) -> int:
    with closing(sqlite3.connect(media_db_path)) as conn:
        row = conn.execute(
            "SELECT rowid FROM Name2Id WHERE user_name = ?", (username,)
        ).fetchone()
    if not row:
        raise RuntimeError(f"media_0.Name2Id 里找不到 {username}")
    return row[0]


def fetch_voice_messages(
    msg_db_path: str, table: str, start_ts: int, end_ts: int
) -> list[dict]:
    """语音消息 (local_type=34)，按时间升序。"""
    with closing(sqlite3.connect(msg_db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT local_id, create_time, real_sender_id, message_content
            FROM [{table}]
            WHERE (local_type & 0xFFFFFFFF) = 34
              AND create_time BETWEEN ? AND ?
            ORDER BY create_time ASC
            """,
            (start_ts, end_ts),
        ).fetchall()
    return [
        {"local_id": r[0], "create_time": r[1], "sender_id": r[2], "content": r[3]}
        for r in rows
    ]


def fetch_voice_blob(
    media_db_path: str, chat_name_id: int, local_id: int
) -> bytes | None:
    with closing(sqlite3.connect(media_db_path)) as conn:
        row = conn.execute(
            "SELECT voice_data FROM VoiceInfo WHERE chat_name_id = ? AND local_id = ?",
            (chat_name_id, local_id),
        ).fetchone()
    return row[0] if row else None


def silk_blob_to_wav(blob: bytes, workdir: Path, stem: str) -> Path:
    """WeChat SILK blob → WAV 16kHz mono.

    WeChat 的 SILK blob 开头是 0x02 + "#!SILK_V3"（前置一个 0x02 字节）。
    pilk 需要跳过这个 0x02，从 "#!SILK_V3" 开始才能解码。
    """
    import pilk

    silk_path = workdir / f"{stem}.silk"
    wav_path = workdir / f"{stem}.wav"

    # Strip the leading 0x02 byte WeChat prepends
    data = blob[1:] if blob.startswith(b"\x02#!SILK_V3") else blob
    silk_path.write_bytes(data)

    pilk.silk_to_wav(str(silk_path), str(wav_path), rate=16000)
    return wav_path


def transcribe_wav(wav_path: Path) -> str:
    """Call DashScope Paraformer-realtime-v2 on a local wav file."""
    from dashscope.audio.asr import Recognition

    recognition = Recognition(
        model="paraformer-realtime-v2",
        format="wav",
        sample_rate=16000,
        language_hints=["zh", "en"],
        callback=None,  # type: ignore[arg-type]  # accepted for sync mode
    )
    result = recognition.call(str(wav_path))
    if result.status_code != 200:
        raise RuntimeError(
            f"DashScope error: status={result.status_code}, "
            f"message={getattr(result, 'message', '')}"
        )
    sentences = result.get_sentence() or []
    parts: list[str] = []
    for s in sentences:
        if isinstance(s, dict):
            parts.append(str(s.get("text", "")))
        elif isinstance(s, str):
            parts.append(s)
    return "".join(parts).strip()


# ---- markdown rewrite ----

_VOICE_LINE_RE = re.compile(
    r"^- \[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (?P<sender>[^:]+): \[语音\] "
)


def rewrite_markdown(
    input_md: Path,
    output_md: Path,
    transcripts: dict[int, str],
    voice_msgs: list[dict],
) -> int:
    """
    Match markdown [语音] lines to voice messages by ordinal position
    (both are time-ordered within the same chat+window), then splice in
    the transcribed text.
    """
    replaced = 0
    voice_iter = iter(voice_msgs)

    def replace_line(match: re.Match[str]) -> str:
        nonlocal replaced
        try:
            msg = next(voice_iter)
        except StopIteration:
            return match.group(0)
        text = transcripts.get(msg["local_id"]) or "(转录失败)"
        prefix = f"- [{match.group('ts')}] {match.group('sender')}: [语音]"
        replaced += 1
        return f"{prefix} {text}  \n"

    out_lines = []
    for line in input_md.read_text(encoding="utf-8").splitlines(keepends=True):
        m = _VOICE_LINE_RE.match(line)
        if m:
            out_lines.append(replace_line(m))
        else:
            out_lines.append(line)
    output_md.write_text("".join(out_lines), encoding="utf-8")
    return replaced


# ---- main ----

def main() -> int:
    load_env(ROOT / ".env")

    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("ERROR: DASHSCOPE_API_KEY not set (put it in .env)", file=sys.stderr)
        return 2
    # dashscope SDK reads DASHSCOPE_API_KEY from env automatically

    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required=True, help="聊天名（群名或备注/昵称）")
    ap.add_argument("--start-time", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end-time", required=True, help="YYYY-MM-DD")
    ap.add_argument("--input", required=True, help="原 markdown 路径")
    ap.add_argument("--output", required=True, help="输出 markdown 路径")
    ap.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="保留 silk/wav 中间文件（调试用）",
    )
    args = ap.parse_args()

    ctx = AppContext()
    username = resolve_username(args.chat, ctx.cache, ctx.decrypted_dir)
    if not username:
        print(f"ERROR: 找不到聊天 {args.chat}", file=sys.stderr)
        return 2
    names = get_contact_names(ctx.cache, ctx.decrypted_dir)
    display = names.get(username, username)
    print(f"聊天: {display} ({username})")

    start_ts = parse_time_bound(args.start_time, is_end=False)
    end_ts = parse_time_bound(args.end_time, is_end=True)

    msg_db, table = find_group_msg_table(ctx, username)
    media_db = find_media_db(ctx)
    chat_name_id = lookup_chat_name_id(media_db, username)
    print(f"msg table: {table} @ {msg_db}")
    print(f"chat_name_id in media_0: {chat_name_id}")

    voice_msgs = fetch_voice_messages(msg_db, table, start_ts, end_ts)
    print(f"语音消息数: {len(voice_msgs)}")

    transcripts: dict[int, str] = {}
    work_root = (
        ROOT / ".tmp_transcribe"
        if args.keep_intermediate
        else Path(tempfile.mkdtemp(prefix="wechat_voice_"))
    )
    work_root.mkdir(parents=True, exist_ok=True)

    for i, msg in enumerate(voice_msgs, 1):
        local_id = msg["local_id"]
        ts_str = datetime.fromtimestamp(msg["create_time"]).strftime("%H:%M:%S")
        blob = fetch_voice_blob(media_db, chat_name_id, local_id)
        if not blob:
            print(f"  [{i}/{len(voice_msgs)}] {ts_str} local_id={local_id} ❌ blob missing")
            transcripts[local_id] = "(语音 blob 缺失)"
            continue
        try:
            wav = silk_blob_to_wav(blob, work_root, f"v_{local_id}")
            text = transcribe_wav(wav)
            transcripts[local_id] = text or "(空)"
            preview = (text or "").replace("\n", " ")[:60]
            print(f"  [{i}/{len(voice_msgs)}] {ts_str} local_id={local_id} ✓ {preview}")
        except Exception as e:
            transcripts[local_id] = f"(转录失败: {e})"
            print(f"  [{i}/{len(voice_msgs)}] {ts_str} local_id={local_id} ✗ {e}")

    replaced = rewrite_markdown(
        Path(args.input), Path(args.output), transcripts, voice_msgs
    )
    print(f"\n✅ 完成: 替换 {replaced}/{len(voice_msgs)} 条，输出 → {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
