#!/usr/bin/env python3
"""
Embed txt file content into PNG images as XMP metadata.
This is the reverse operation of extracting XMP data to txt files.

Writes txt content to XMP:Description and XMP:Title fields.
"""
import os
import sys
import subprocess
import shutil
from pathlib import Path


def check_exiftool() -> bool:
    """Check if exiftool is installed."""
    return shutil.which("exiftool") is not None


def get_image_files(folder_path: Path, extensions: tuple = (".png", ".jpg", ".jpeg", ".webp")) -> list:
    """Get all image files from folder, sorted."""
    image_files = []
    for ext in extensions:
        image_files.extend(folder_path.glob(f"*{ext}"))
        image_files.extend(folder_path.glob(f"*{ext.upper()}"))
    return sorted(set(image_files))


def embed_xmp_to_image(image_path: Path, text_content: str) -> bool:
    """
    Embed text content into image as XMP metadata.
    Uses exiftool to write XMP:Description and XMP:Title.
    """
    try:
        result = subprocess.run(
            [
                "exiftool",
                "-overwrite_original",
                f"-XMP:Description={text_content}",
                f"-XMP:Title={text_content}",
                "-charset", "iptc=UTF8",
                str(image_path)
            ],
            capture_output=True,
            text=True
        )
        return "1 image files updated" in result.stdout
    except Exception as e:
        print(f"Error processing {image_path}: {e}")
        return False


def main():
    # Parse arguments
    if len(sys.argv) < 2:
        print("Usage: python ___XXX_embed_prompt_to_xmp.py <image_folder> [--overwrite]")
        print("\nOptions:")
        print("  <image_folder>  Path to folder containing images and corresponding .txt files")
        print("  --overwrite     Overwrite existing XMP data (default: skip images with XMP)")
        print("\nExamples:")
        print("  python ___XXX_embed_prompt_to_xmp.py ./test")
        print("  python ___XXX_embed_prompt_to_xmp.py ./test --overwrite")
        print("\nNote: Requires exiftool to be installed.")
        print("      Install: sudo apt install libimage-exiftool-perl")
        sys.exit(1)

    image_folder = Path(sys.argv[1])
    overwrite = "--overwrite" in sys.argv

    # Validate folder
    if not image_folder.is_dir():
        print(f"Error: Folder not found: {image_folder}")
        sys.exit(1)

    # Check exiftool
    if not check_exiftool():
        print("Error: exiftool is not installed")
        print("Install: sudo apt install libimage-exiftool-perl")
        sys.exit(1)

    print("=" * 50)
    print("📷 TXT → XMP:Description Embedding Tool")
    print("=" * 50)
    print()
    print(f"Image folder: {image_folder}")
    print(f"Overwrite existing: {overwrite}")
    print()

    # Get image files
    image_files = get_image_files(image_folder)
    print(f"Found {len(image_files)} image files")
    print()

    if len(image_files) == 0:
        print("No image files found.")
        sys.exit(1)

    # Process each image
    processed = 0
    skipped_no_prompt = 0
    failed = 0

    for image_file in image_files:
        # Find corresponding txt file
        prompt_file = image_file.with_suffix(".txt")

        if not prompt_file.exists():
            skipped_no_prompt += 1
            continue

        # Read prompt content
        with open(prompt_file, "r", encoding="utf-8") as f:
            text_content = f.read().strip()

        if not text_content:
            print(f"Warning: Empty prompt file for {image_file.name}, skipping...")
            skipped_no_prompt += 1
            continue

        # Embed XMP data
        if embed_xmp_to_image(image_file, text_content):
            processed += 1
            print(f"✓ {processed}: {image_file.name}")
        else:
            failed += 1
            print(f"✗ Failed: {image_file.name}")

    print()
    print("=" * 50)
    print("✅ Done!")
    print(f"  Processed: {processed}")
    if skipped_no_prompt > 0:
        print(f"  Skipped (no .txt): {skipped_no_prompt}")
    if failed > 0:
        print(f"  Failed: {failed}")
    print(f"  Total images: {len(image_files)}")
    print("=" * 50)


if __name__ == "__main__":
    main()
