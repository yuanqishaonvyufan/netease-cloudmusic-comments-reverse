"""网易云音乐网页评论接口逆向教学模板。

本程序使用 PyExecJS 调用同目录的 main.js，生成网页接口要求的
params 和 encSecKey，再按 cursor 顺序翻页保存评论。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qs, urlparse

import execjs
import requests


DEFAULT_SONG_ID = 204072
DEFAULT_PAGE_SIZE = 20
API_URL = "https://music.163.com/weapi/comment/resource/comments/get"
PROJECT_DIR = Path(__file__).resolve().parent
MAIN_JS = PROJECT_DIR / "main.js"
OUTPUT_FIELDS = [
    "comment_id",
    "user_id",
    "username",
    "content",
    "liked_count",
    "reply_count",
    "ip_location",
    "timestamp",
    "publish_time",
]


class CrawlerError(RuntimeError):
    """爬虫可预期错误的基类。"""


class EncryptError(CrawlerError):
    """JavaScript 编译或加密失败。"""


class ApiError(CrawlerError):
    """网易云接口请求或业务响应异常。"""


@dataclass(frozen=True)
class Credentials:
    cookie: str = ""
    csrf_token: str = ""


@dataclass(frozen=True)
class PageResult:
    comments: list[dict[str, Any]]
    cursor: Any
    total_count: int
    has_more: bool | None


def extract_cookie_value(cookie_text: str, name: str) -> str:
    """从浏览器复制的 Cookie 请求头中提取指定字段。"""
    for item in cookie_text.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == name:
            return value
    return ""


def load_credentials() -> Credentials:
    """读取身份信息，环境变量优先于本地 config.py。"""
    config_cookie = ""
    config_csrf = ""
    try:
        import config  # type: ignore
    except ModuleNotFoundError as exc:
        if exc.name != "config":
            raise
    else:
        config_cookie = str(getattr(config, "COOKIE", "") or "").strip()
        config_csrf = str(getattr(config, "CSRF_TOKEN", "") or "").strip()

    cookie = os.getenv("NETEASE_COOKIE", config_cookie).strip()
    csrf_token = os.getenv("NETEASE_CSRF_TOKEN", config_csrf).strip()
    csrf_token = csrf_token or extract_cookie_value(cookie, "__csrf")
    return Credentials(cookie=cookie, csrf_token=csrf_token)


def parse_song_id(value: str | int) -> int:
    """同时接受纯歌曲 ID 和 music.163.com 歌曲链接。"""
    if isinstance(value, int):
        song_id = value
    else:
        text = value.strip()
        if text.isdigit():
            song_id = int(text)
        else:
            parsed = urlparse(text)
            ids = parse_qs(parsed.query).get("id", [])
            if not ids and parsed.fragment:
                fragment_query = parsed.fragment.split("?", 1)[-1]
                ids = parse_qs(fragment_query).get("id", [])
            if not ids or not ids[0].isdigit():
                raise ValueError(f"无法从输入中识别歌曲 ID：{value}")
            song_id = int(ids[0])

    if song_id <= 0:
        raise ValueError("歌曲 ID 必须是正整数。")
    return song_id


def build_page_data(
    song_id: int,
    page_no: int,
    page_size: int,
    cursor: Any,
    csrf_token: str,
) -> dict[str, Any]:
    """按网页抓包结构构造加密前的 i8K。"""
    resource_id = f"R_SO_4_{song_id}"
    return {
        "rid": resource_id,
        "threadId": resource_id,
        "pageNo": page_no,
        "pageSize": page_size,
        "cursor": -1 if page_no == 1 else cursor,
        "offset": 0,
        "orderType": 1,
        "csrf_token": csrf_token,
    }


def parse_comment(comment: dict[str, Any]) -> dict[str, Any]:
    """把接口中的嵌套评论对象整理为适合保存的一行数据。"""
    user = comment.get("user") or {}
    ip_location = comment.get("ipLocation") or {}
    timestamp = comment.get("time")
    readable_time = comment.get("timeStr") or ""
    if not readable_time and isinstance(timestamp, (int, float)):
        readable_time = datetime.fromtimestamp(timestamp / 1000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    return {
        "comment_id": comment.get("commentId", ""),
        "user_id": user.get("userId", ""),
        "username": user.get("nickname", ""),
        "content": comment.get("content", ""),
        "liked_count": comment.get("likedCount", 0),
        "reply_count": comment.get("replyCount", 0),
        "ip_location": ip_location.get("location", ""),
        "timestamp": timestamp or "",
        "publish_time": readable_time,
    }


def sanitize_filename(value: str) -> str:
    """清理用户传入的输出文件名片段。"""
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("._") or "comments"


def load_js_context(js_path: Path = MAIN_JS):
    if not js_path.exists():
        raise FileNotFoundError(f"找不到加密文件：{js_path}")
    try:
        return execjs.compile(js_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise EncryptError(
            "main.js 编译失败。请确认 Node.js 已安装，并运行 node --check main.js 排查语法。"
        ) from exc


def encrypt_page(js_context, page_data: dict[str, Any]) -> dict[str, str]:
    """调用 main.js 的 getData(i8K)，获取 POST 表单的两个密文字段。"""
    try:
        encrypted = js_context.call("getData", page_data)
    except Exception as exc:
        raise EncryptError(f"调用 getData(i8K) 失败：{exc}") from exc

    if not isinstance(encrypted, dict):
        raise EncryptError("getData() 没有返回对象。")
    params = encrypted.get("params")
    enc_sec_key = encrypted.get("encSecKey")
    if not isinstance(params, str) or not isinstance(enc_sec_key, str):
        raise EncryptError("getData() 返回结果缺少 params 或 encSecKey。")
    return {"params": params, "encSecKey": enc_sec_key}


def create_session(cookie: str, song_id: int) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://music.163.com",
            "Referer": f"https://music.163.com/song?id={song_id}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        }
    )
    if cookie:
        session.headers["Cookie"] = cookie
    return session


class CommentWriter:
    """增量写入 CSV 或 JSONL，异常中断时也能保留已经抓到的数据。"""

    def __init__(self, path: Path, output_format: str):
        self.path = path
        self.output_format = output_format
        self.file: TextIO | None = None
        self.csv_writer: csv.DictWriter | None = None

    def __enter__(self) -> "CommentWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoding = "utf-8-sig" if self.output_format == "csv" else "utf-8"
        self.file = self.path.open("w", newline="", encoding=encoding)
        if self.output_format == "csv":
            self.csv_writer = csv.DictWriter(self.file, fieldnames=OUTPUT_FIELDS)
            self.csv_writer.writeheader()
        return self

    def write(self, comment: dict[str, Any]) -> None:
        if self.file is None:
            raise RuntimeError("输出文件尚未打开。")
        if self.csv_writer is not None:
            self.csv_writer.writerow(comment)
        else:
            self.file.write(json.dumps(comment, ensure_ascii=False) + "\n")

    def flush(self) -> None:
        if self.file is not None:
            self.file.flush()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.file is not None:
            self.file.close()


class NeteaseCommentCrawler:
    def __init__(
        self,
        song_id: int,
        credentials: Credentials,
        retries: int = 3,
        timeout: float = 20.0,
    ):
        self.song_id = song_id
        self.credentials = credentials
        self.retries = retries
        self.timeout = timeout
        self.js_context = load_js_context()
        self.session = create_session(credentials.cookie, song_id)

    def close(self) -> None:
        self.session.close()

    def request_page(self, page_data: dict[str, Any]) -> PageResult:
        """加密并请求一页；网络错误会退避重试，业务错误立即报出。"""
        encrypted = encrypt_page(self.js_context, page_data)
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.post(
                    API_URL,
                    params={"csrf_token": self.credentials.csrf_token},
                    data=encrypted,
                    timeout=(5, self.timeout),
                )
                response.raise_for_status()
                result = response.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(attempt * 1.5)
                    continue
                raise ApiError(
                    f"请求连续 {self.retries} 次超时或连接失败：{exc}"
                ) from exc
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                raise ApiError(f"HTTP 请求失败：status={status}，{exc}") from exc
            except requests.JSONDecodeError as exc:
                preview = response.text[:200].replace("\n", " ")
                raise ApiError(f"接口没有返回 JSON，响应开头：{preview}") from exc

            if result.get("code") != 200:
                message = result.get("message") or result.get("msg") or "未知错误"
                raise ApiError(
                    f"接口业务错误：code={result.get('code')}，message={message}"
                )

            data = result.get("data")
            if not isinstance(data, dict):
                raise ApiError("接口返回 code=200，但缺少 data 对象。")
            raw_comments = data.get("comments") or []
            if not isinstance(raw_comments, list):
                raise ApiError("接口 data.comments 不是列表。")

            has_more_value = data.get("more", data.get("hasMore"))
            has_more = (
                bool(has_more_value) if has_more_value is not None else None
            )
            return PageResult(
                comments=raw_comments,
                cursor=data.get("cursor"),
                total_count=int(data.get("totalCount") or 0),
                has_more=has_more,
            )

        raise ApiError(f"请求失败：{last_error}")

    def crawl(
        self,
        page_size: int,
        max_pages: int,
        sleep_seconds: float,
        jitter: float,
        output_path: Path,
        output_format: str,
    ) -> int:
        page_no = 1
        cursor: Any = -1
        expected_total = 0
        seen_ids: set[Any] = set()
        seen_cursors: set[str] = set()
        saved_count = 0
        reached_last_page = False

        with CommentWriter(output_path, output_format) as writer:
            while max_pages <= 0 or page_no <= max_pages:
                page_data = build_page_data(
                    self.song_id,
                    page_no,
                    page_size,
                    cursor,
                    self.credentials.csrf_token,
                )
                page = self.request_page(page_data)

                if not expected_total and page.total_count:
                    expected_total = page.total_count
                    expected_pages = math.ceil(expected_total / page_size)
                    print(f"评论总数（接口估计）：{expected_total}，约 {expected_pages} 页")

                if not page.comments:
                    print(f"第 {page_no} 页为空，停止翻页。")
                    reached_last_page = True
                    break

                page_saved = 0
                for raw_comment in page.comments:
                    parsed = parse_comment(raw_comment)
                    comment_id = parsed["comment_id"]
                    if comment_id and comment_id in seen_ids:
                        continue
                    if comment_id:
                        seen_ids.add(comment_id)
                    writer.write(parsed)
                    page_saved += 1
                    saved_count += 1
                writer.flush()

                print(
                    f"第 {page_no} 页：收到 {len(page.comments)} 条，"
                    f"新增 {page_saved} 条，累计 {saved_count} 条"
                )

                # totalCount/hasMore 偶尔会与实际页数短暂不一致。顺序爬取时，
                # “末页数量不足 pageSize”比统计字段更可靠。
                if len(page.comments) < page_size:
                    reached_last_page = True
                    break
                if page.cursor is None:
                    print("响应没有 cursor，停止以避免重复请求。")
                    break
                cursor_marker = str(page.cursor)
                if cursor_marker in seen_cursors or page_saved == 0:
                    print("cursor 或评论开始重复，停止以避免死循环。")
                    break

                seen_cursors.add(cursor_marker)
                cursor = page.cursor
                page_no += 1
                if sleep_seconds > 0 or jitter > 0:
                    delay = sleep_seconds + random.uniform(0, jitter)
                    time.sleep(delay)

        if max_pages > 0 and page_no >= max_pages and not reached_last_page:
            print(f"已达到 --max-pages={max_pages}，本次按设置停止。")
        if reached_last_page and expected_total and saved_count != expected_total:
            print(
                f"提示：接口最初估计 {expected_total} 条，实际保存 {saved_count} 条。"
                "评论实时变化或统计缓存可能造成少量差异。"
            )
        return saved_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="网易云音乐网页评论接口逆向教学模板",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "song",
        nargs="?",
        default=str(DEFAULT_SONG_ID),
        help="歌曲 ID 或 https://music.163.com/song?id=... 链接",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="最多抓取页数；0 表示抓到末页",
    )
    parser.add_argument("--sleep", type=float, default=1.0, help="固定页间隔秒数")
    parser.add_argument("--jitter", type=float, default=0.5, help="随机附加间隔秒数")
    parser.add_argument("--retries", type=int, default=3, help="网络错误重试次数")
    parser.add_argument("--timeout", type=float, default=20.0, help="读取超时秒数")
    parser.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    parser.add_argument("--output", type=Path, help="自定义输出文件路径")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只测试 JS 加密，不发送网络请求",
    )
    return parser


def validate_args(args: argparse.Namespace) -> int:
    try:
        song_id = parse_song_id(args.song)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not 1 <= args.page_size <= 20:
        raise SystemExit("--page-size 应在 1 到 20 之间。")
    if args.max_pages < 0:
        raise SystemExit("--max-pages 不能小于 0。")
    if args.sleep < 0 or args.jitter < 0:
        raise SystemExit("--sleep 和 --jitter 不能小于 0。")
    if args.retries < 1:
        raise SystemExit("--retries 不能小于 1。")
    if args.timeout <= 0:
        raise SystemExit("--timeout 必须大于 0。")
    return song_id


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    song_id = validate_args(args)
    credentials = load_credentials()

    output_path = args.output
    if output_path is None:
        filename = sanitize_filename(f"comments_{song_id}.{args.format}")
        output_path = PROJECT_DIR / "output" / filename

    try:
        if args.dry_run:
            payload = build_page_data(
                song_id, 1, args.page_size, -1, credentials.csrf_token
            )
            encrypted = encrypt_page(load_js_context(), payload)
            print("JS 加密测试通过")
            print(f"params 长度：{len(encrypted['params'])}")
            print(f"encSecKey 长度：{len(encrypted['encSecKey'])}")
            return 0

        crawler = NeteaseCommentCrawler(
            song_id=song_id,
            credentials=credentials,
            retries=args.retries,
            timeout=args.timeout,
        )
        try:
            count = crawler.crawl(
                page_size=args.page_size,
                max_pages=args.max_pages,
                sleep_seconds=args.sleep,
                jitter=args.jitter,
                output_path=output_path,
                output_format=args.format,
            )
        finally:
            crawler.close()
    except (FileNotFoundError, CrawlerError) as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1

    print(f"完成：共保存 {count} 条评论 -> {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
