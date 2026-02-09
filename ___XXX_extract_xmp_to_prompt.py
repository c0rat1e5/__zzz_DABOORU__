#!/usr/bin/env python3
"""
Extract XMP data from images and save as txt files with the same name.
This is the reverse operation of embedding text into XMP data.
"""
import os
import re
import sys
import html
from pathlib import Path
from PIL import Image


def extract_xmp_description(xmp_data: str) -> str:
    """
    Extract the description text from XMP data.
    Looks for dc:description content.
    """
    if isinstance(xmp_data, bytes):
        xmp_data = xmp_data.decode("utf-8", errors="ignore")

    # Try to find dc:description content
    # Pattern: <dc:description>...<rdf:li xml:lang='x-default'>CONTENT</rdf:li>...</dc:description>
    pattern = r"<dc:description>.*?<rdf:li[^>]*>(.+?)</rdf:li>.*?</dc:description>"
    match = re.search(pattern, xmp_data, re.DOTALL)

    if match:
        content = match.group(1).strip()
        # Unescape HTML entities (e.g., &lt; -> <, &gt; -> >)
        content = html.unescape(content)
        return content

    # Alternative: try dc:title if dc:description not found
    pattern = r"<dc:title>.*?<rdf:li[^>]*>(.+?)</rdf:li>.*?</dc:title>"
    match = re.search(pattern, xmp_data, re.DOTALL)

    if match:
        content = match.group(1).strip()
        content = html.unescape(content)
        return content

    return ""


def get_image_files(folder_path: Path, extensions: tuple = (".png", ".jpg", ".jpeg", ".webp")) -> list:
    """Get all image files from folder, sorted."""
    image_files = []
    for ext in extensions:
        image_files.extend(folder_path.glob(f"*{ext}"))
        image_files.extend(folder_path.glob(f"*{ext.upper()}"))
    return sorted(set(image_files))


def extract_xmp_from_image(image_path: Path) -> str:
    """Extract XMP description from an image file."""
    try:
        with Image.open(image_path) as img:
            # Check for XMP data in image info
            xmp_data = None

            # Try different possible XMP keys
            for key in ["xmp", "XML:com.adobe.xmp", "XMP"]:
                if key in img.info:
                    xmp_data = img.info[key]
                    break

            if xmp_data:
                return extract_xmp_description(xmp_data)

            return ""
    except Exception as e:
        print(f"Error reading {image_path}: {e}")
        return ""


def main():
    # Parse arguments
    if len(sys.argv) < 2:
        print("Usage: python ___XXX_extract_xmp_to_prompt.py <image_folder> [--overwrite]")
        print("\nOptions:")
        print("  <image_folder>  Path to folder containing images with XMP data")
        print("  --overwrite     Overwrite existing .txt files (default: skip)")
        print("\nExamples:")
        print("  python ___XXX_extract_xmp_to_prompt.py ./test")
        print("  python ___XXX_extract_xmp_to_prompt.py ./test --overwrite")
        sys.exit(1)

    image_folder = Path(sys.argv[1])
    overwrite = "--overwrite" in sys.argv

    # Validate folder
    if not image_folder.is_dir():
        print(f"Error: Folder not found: {image_folder}")
        sys.exit(1)

    print("=" * 50)
    print("📷 XMP:Description → TXT Extraction Tool")
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
    skipped = 0
    no_xmp = 0

    for image_file in image_files:
        # Create prompt file path with same name (.txt)
        prompt_file = image_file.with_suffix(".txt")

        # Check if txt file already exists
        if prompt_file.exists() and not overwrite:
            print(f"Skipping (txt exists): {image_file.name}")
            skipped += 1
            continue

        # Extract XMP data
        xmp_content = extract_xmp_from_image(image_file)

        if not xmp_content:
            print(f"No XMP data found: {image_file.name}")
            no_xmp += 1
            continue

        # Write prompt file
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(xmp_content)

        print(f"✓ Created: {prompt_file.name}")
        processed += 1

    print()
    print("=" * 50)
    print("✅ Done!")
    print(f"  Processed: {processed}")
    if skipped > 0:
        print(f"  Skipped (txt exists): {skipped}")
    if no_xmp > 0:
        print(f"  No XMP data: {no_xmp}")
    print(f"  Total images: {len(image_files)}")
    print("=" * 50)


if __name__ == "__main__":
    main()
