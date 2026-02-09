#!/usr/bin/env python3
"""
Danbooru Multi-Tag Search & Download (Gradio UI)
- 2タグ制限を回避: APIに2タグ送信 → Python側で残りをフィルタ
- Gradio でブラウザ上からタグ検索・プレビュー・ダウンロード
- SDXL リサイズ + 平均色パディング
- XMP メタデータ埋め込み

使い方:
  python danbooru_search_ui.py
  → ブラウザで http://localhost:7860 を開く
"""
import json
import os
import re
import sys
import time
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import requests
import numpy as np
from PIL import Image
import gradio as gr

# ============================================================
# 設定
# ============================================================
DANBOORU_LOGIN = "palm_floods"
DANBOORU_API_KEY = "Vsq3KWK3pCUGbVPwnDSUtRXF"
PER_PAGE = 200
BASE_URL = "https://danbooru.donmai.us"
AUTH = (DANBOORU_LOGIN, DANBOORU_API_KEY)

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "danbooru_search_results"

# SDXL 標準解像度
SDXL_RESOLUTIONS = [
    (1024, 1024),
    (1152, 896),
    (896, 1152),
    (1216, 832),
    (832, 1216),
    (1344, 768),
    (768, 1344),
    (1536, 640),
    (640, 1536),
]

ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}


# ============================================================
# Danbooru API 検索 (2タグ制限回避)
# ============================================================
def search_danbooru(
    tags_input: str,
    max_results: int = 100,
    rating_filter: str = "all",
    min_score: int = 0,
) -> tuple:
    """
    複数タグ検索:
    - 最初の2タグを API に送信
    - 残りのタグは Python 側でフィルタリング
    - 除外タグ (-tag) にも対応
    """
    tags = tags_input.strip().split()
    if not tags:
        return [], "タグを入力してください"

    # +タグ と -タグ を分離
    include_tags = [t for t in tags if not t.startswith("-")]
    exclude_tags = [t.lstrip("-") for t in tags if t.startswith("-")]

    # API に送る2タグ (最初の2つ)
    api_tags = include_tags[:2]
    # Python 側でフィルタする追加タグ
    extra_include = include_tags[2:]

    session = requests.Session()
    session.auth = AUTH
    session.headers["User-Agent"] = "DanbooruSearchUI/1.0"

    all_posts = []
    seen_ids = set()
    page = 1
    api_tag_str = " ".join(api_tags)

    status_log = f'API検索: "{api_tag_str}"\n'
    if extra_include:
        status_log += f"追加フィルタ: {', '.join(extra_include)}\n"
    if exclude_tags:
        status_log += f"除外タグ: {', '.join(exclude_tags)}\n"

    # 十分な結果を得るために多めに取得
    fetch_limit = max_results * 5 if extra_include else max_results * 2
    fetch_limit = min(fetch_limit, 5000)

    while len(all_posts) < fetch_limit:
        params = {"tags": api_tag_str, "limit": PER_PAGE, "page": page}
        resp = session.get(f"{BASE_URL}/posts.json", params=params)

        if resp.status_code != 200:
            status_log += f"API Error: HTTP {resp.status_code}\n"
            break

        posts = resp.json()
        if not posts:
            break

        for p in posts:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_posts.append(p)

        if len(posts) < PER_PAGE:
            break
        page += 1
        time.sleep(0.3)

    status_log += f"API取得: {len(all_posts)} posts\n"

    # --- Python 側フィルタリング ---
    filtered = []
    for p in all_posts:
        post_tags = set(p.get("tag_string", "").split())
        rating = p.get("rating", "")
        score = p.get("score", 0)
        ext = p.get("file_ext", "")

        # 画像のみ
        if ext.lower() not in ALLOWED_EXT:
            continue

        # 追加タグフィルタ
        if extra_include and not all(t in post_tags for t in extra_include):
            continue

        # 除外タグ
        if exclude_tags and any(t in post_tags for t in exclude_tags):
            continue

        # Rating フィルタ
        if rating_filter != "all":
            if rating_filter == "safe" and rating != "g":
                continue
            elif rating_filter == "sensitive" and rating != "s":
                continue
            elif rating_filter == "questionable" and rating != "q":
                continue
            elif rating_filter == "explicit" and rating != "e":
                continue

        # スコアフィルタ
        if score < min_score:
            continue

        filtered.append(p)

        if len(filtered) >= max_results:
            break

    status_log += f"フィルタ後: {len(filtered)} posts\n"

    return filtered, status_log


# ============================================================
# プレビュー画像取得
# ============================================================
def get_preview_data(posts: list) -> list:
    """各投稿のプレビュー画像をダウンロードしてギャラリー用リストを返す"""
    import tempfile
    preview_dir = Path(tempfile.gettempdir()) / "danbooru_previews"
    preview_dir.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "DanbooruSearchUI/1.0"

    gallery_items = []
    for p in posts:
        pid = p["id"]
        # プレビューURL (小さい画像)
        preview_url = (
            p.get("preview_file_url")
            or p.get("large_file_url")
            or p.get("file_url", "")
        )
        if not preview_url:
            continue

        # ローカルにキャッシュ
        ext = preview_url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
        local_path = preview_dir / f"{pid}.{ext}"
        if not local_path.exists():
            try:
                r = session.get(preview_url, timeout=10)
                if r.status_code == 200:
                    local_path.write_bytes(r.content)
                else:
                    continue
            except:
                continue

        # キャプション
        tags_short = p.get("tag_string", "")[:100]
        rating = p.get("rating", "?")
        score = p.get("score", 0)
        caption = f"#{pid} | r:{rating} s:{score} | {tags_short}..."

        gallery_items.append((str(local_path), caption))

    return gallery_items


# ============================================================
# SDXL リサイズ
# ============================================================
def find_closest_sdxl_resolution(w: int, h: int) -> tuple:
    aspect = w / h
    best = min(SDXL_RESOLUTIONS, key=lambda r: abs((r[0] / r[1]) - aspect))
    return best


def resize_to_sdxl(filepath: Path) -> bool:
    try:
        img = Image.open(filepath).convert("RGB")
        orig_w, orig_h = img.size
        target_w, target_h = find_closest_sdxl_resolution(orig_w, orig_h)

        if orig_w == target_w and orig_h == target_h:
            return True

        # 平均色
        arr = np.array(img)
        avg_color = tuple(arr.mean(axis=(0, 1)).astype(int))

        # bicubic リサイズ (fit inside)
        scale = min(target_w / orig_w, target_h / orig_h)
        new_w = round(orig_w * scale)
        new_h = round(orig_h * scale)
        img_resized = img.resize((new_w, new_h), Image.BICUBIC)

        # 平均色パディング
        canvas = Image.new("RGB", (target_w, target_h), avg_color)
        paste_x = (target_w - new_w) // 2
        paste_y = (target_h - new_h) // 2
        canvas.paste(img_resized, (paste_x, paste_y))

        ext = filepath.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            canvas.save(filepath, "JPEG", quality=95)
        elif ext == ".png":
            canvas.save(filepath, "PNG")
        elif ext == ".webp":
            canvas.save(filepath, "WEBP", quality=95)
        else:
            canvas.save(filepath)
        return True
    except Exception as e:
        print(f"Resize error: {e}")
        return False


# ============================================================
# XMP 埋め込み
# ============================================================
def embed_xmp(filepath: Path, tags_str: str, rating: str, score: int) -> bool:
    if not shutil.which("exiftool"):
        return False
    full_desc = f"{tags_str} rating:{rating} score:{score}"
    cmd = [
        "exiftool",
        "-overwrite_original",
        f"-XMP:Description={full_desc}",
        f"-XMP:Title={full_desc}",
        "-charset",
        "iptc=UTF8",
    ]
    for tag in tags_str.split():
        cmd.append(f"-XMP:Subject+={tag}")
    cmd += [f"-XMP:Subject+=rating:{rating}", f"-XMP:Subject+=score:{score}"]
    cmd.append(str(filepath))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return "1 image files updated" in result.stdout
    except:
        return False


# ============================================================
# ダウンロード＆処理
# ============================================================
def download_selected(
    posts_json: str,
    do_resize: bool,
    do_xmp: bool,
    output_folder: str,
    progress=gr.Progress(),
) -> str:
    """選択された投稿の画像をダウンロード・リサイズ・XMP埋め込み"""
    if not posts_json:
        return "データがありません。まず検索してください。"

    posts = json.loads(posts_json)
    if not posts:
        return "投稿がありません。"

    out_dir = Path(output_folder) if output_folder else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "DanbooruSearchUI/1.0"

    log = f"出力先: {out_dir}\n"
    downloaded = 0
    resized = 0
    xmp_count = 0

    for i, p in enumerate(progress.tqdm(posts, desc="Downloading")):
        pid = p["id"]
        ext = p.get("file_ext", "jpg")
        file_url = p.get("file_url") or p.get("large_file_url") or ""
        tags_str = p.get("tag_string", "").replace(" ", ", ")

        if not file_url:
            continue

        fp = out_dir / f"{pid}.{ext}"

        # ダウンロード
        if not fp.exists():
            try:
                r = session.get(file_url, stream=True, timeout=60)
                if r.status_code == 200:
                    with open(fp, "wb") as f:
                        for chunk in r.iter_content(8192):
                            f.write(chunk)
                    downloaded += 1
                else:
                    log += f"#{pid}: HTTP {r.status_code}\n"
                    continue
            except Exception as e:
                log += f"#{pid}: Error {e}\n"
                continue
            time.sleep(0.2)

        # SDXL リサイズ
        if do_resize and fp.exists():
            if resize_to_sdxl(fp):
                resized += 1

        # XMP 埋め込み
        if do_xmp and fp.exists():
            rating = p.get("rating", "")
            score = p.get("score", 0)
            if embed_xmp(fp, tags_str, rating, score):
                xmp_count += 1

    # JSON メタデータ保存
    metadata = []
    for p in posts:
        ext = p.get("file_ext", "jpg")
        if ext.lower() not in ALLOWED_EXT:
            continue
        metadata.append(
            {
                "data-id": p["id"],
                "data-tags": p.get("tag_string", "").replace(" ", ", "),
                "data-rating": p.get("rating", ""),
                "data-score": p.get("score", 0),
                "data-uploader-id": p.get("uploader_id", 0),
                "file_url": p.get("file_url", ""),
                "file_ext": ext,
                "source": p.get("source", ""),
                "tag_string_artist": p.get("tag_string_artist", "").replace(" ", ", "),
                "tag_string_character": p.get("tag_string_character", "").replace(
                    " ", ", "
                ),
                "tag_string_copyright": p.get("tag_string_copyright", "").replace(
                    " ", ", "
                ),
            }
        )

    json_path = out_dir / "_search_metadata.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    log += f"\nDone!\n"
    log += f"  Downloaded: {downloaded}\n"
    log += f"  Resized:    {resized}\n"
    log += f"  XMP:        {xmp_count}\n"
    log += f"  JSON:       {json_path.name}\n"
    log += f"  Folder:     {out_dir}\n"
    return log


# ============================================================
# Gradio による検索・フィルタ状態の保持
# ============================================================
_current_posts = []


def do_search(tags: str, max_results: int, rating: str, min_score: int):
    """検索を実行してギャラリーとステータスを返す"""
    global _current_posts

    posts, status = search_danbooru(tags, max_results, rating, min_score)
    _current_posts = posts

    gallery_items = get_preview_data(posts)
    posts_json = json.dumps(posts)

    return gallery_items, status, posts_json


def do_download(posts_json: str, do_resize: bool, do_xmp: bool, output_folder: str):
    """ダウンロードを実行"""
    return download_selected(posts_json, do_resize, do_xmp, output_folder)


# ============================================================
# Gradio UI
# ============================================================
def create_ui():
    with gr.Blocks(
        title="Danbooru Multi-Tag Search",
    ) as app:
        gr.Markdown("# Danbooru Multi-Tag Search")
        gr.Markdown(
            "2タグ制限を回避！ 複数タグで検索 → プレビュー → ダウンロード\n\n"
            "**除外タグ**: `-tag` で除外 (例: `1girl blue_hair -comic`)"
        )

        posts_state = gr.State("")

        with gr.Row():
            with gr.Column(scale=3):
                tags_input = gr.Textbox(
                    label="タグ (スペース区切り・何個でもOK)",
                    placeholder="1girl blue_hair large_breasts highres solo -comic -monochrome",
                    lines=2,
                )
            with gr.Column(scale=1):
                max_results = gr.Slider(
                    minimum=10, maximum=1000, value=200, step=10, label="最大件数"
                )
                min_score = gr.Number(value=0, label="最低スコア", precision=0)

        with gr.Row():
            rating_filter = gr.Radio(
                choices=["all", "safe", "sensitive", "questionable", "explicit"],
                value="all",
                label="Rating フィルタ",
            )
            search_btn = gr.Button("🔍 検索", variant="primary", size="lg")

        status_text = gr.Textbox(label="検索ステータス", interactive=False, lines=4)

        gallery = gr.Gallery(
            label="検索結果",
            columns=5,
            rows=4,
            height="auto",
            object_fit="contain",
        )

        gr.Markdown("---")
        gr.Markdown("### ダウンロード設定")

        with gr.Row():
            do_resize = gr.Checkbox(
                value=True, label="SDXL リサイズ (平均色パディング)"
            )
            do_xmp = gr.Checkbox(value=True, label="XMP タグ埋め込み (exiftool)")

        output_folder = gr.Textbox(
            label="保存先フォルダ",
            value=str(DEFAULT_OUTPUT_DIR),
        )

        with gr.Row():
            download_btn = gr.Button("⬇️ 全件ダウンロード", variant="primary", size="lg")

        download_log = gr.Textbox(label="ダウンロードログ", interactive=False, lines=8)

        # イベント接続
        search_btn.click(
            fn=do_search,
            inputs=[tags_input, max_results, rating_filter, min_score],
            outputs=[gallery, status_text, posts_state],
        )

        # Enter キーでも検索
        tags_input.submit(
            fn=do_search,
            inputs=[tags_input, max_results, rating_filter, min_score],
            outputs=[gallery, status_text, posts_state],
        )

        download_btn.click(
            fn=do_download,
            inputs=[posts_state, do_resize, do_xmp, output_folder],
            outputs=[download_log],
        )

    return app


# ============================================================
# メイン
# ============================================================
if __name__ == "__main__":
    app = create_ui()
    app.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        inbrowser=True,
    )
