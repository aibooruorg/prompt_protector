#!/usr/bin/env python
import io
import json
import math
import os
import tempfile
from typing import Optional
import uuid
import zipfile
import gzip
import PIL.Image
import flask
import httpx
from flask import Flask, request, render_template
from werkzeug.datastructures import FileStorage


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["JSON_PRETTYPRINT_REGULAR"] = True


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


def read_info_from_image_stealth(image: PIL.Image.Image) -> str:
    if "parameters" in image.text:
        return image.text["parameters"]

    # trying to read stealth pnginfo
    width, height = image.size
    pixels = image.load()

    has_alpha = True if image.mode == 'RGBA' else False
    mode = None
    compressed = False
    binary_data = ''
    buffer_a = ''
    buffer_rgb = ''
    index_a = 0
    index_rgb = 0
    sig_confirmed = False
    confirming_signature = True
    reading_param_len = False
    reading_param = False
    read_end = False
    for x in range(width):
        for y in range(height):
            if has_alpha:
                r, g, b, a = pixels[x, y]
                buffer_a += str(a & 1)
                index_a += 1
            else:
                r, g, b = pixels[x, y]
            buffer_rgb += str(r & 1)
            buffer_rgb += str(g & 1)
            buffer_rgb += str(b & 1)
            index_rgb += 3
            if confirming_signature:
                if index_a == len('stealth_pnginfo') * 8:
                    decoded_sig = bytearray(int(buffer_a[i:i + 8], 2) for i in
                                            range(0, len(buffer_a), 8)).decode('utf-8', errors='ignore')
                    if decoded_sig in {'stealth_pnginfo', 'stealth_pngcomp'}:
                        confirming_signature = False
                        sig_confirmed = True
                        reading_param_len = True
                        mode = 'alpha'
                        if decoded_sig == 'stealth_pngcomp':
                            compressed = True
                        buffer_a = ''
                        index_a = 0
                    else:
                        read_end = True
                        break
                elif index_rgb == len('stealth_pnginfo') * 8:
                    decoded_sig = bytearray(int(buffer_rgb[i:i + 8], 2) for i in
                                            range(0, len(buffer_rgb), 8)).decode('utf-8', errors='ignore')
                    if decoded_sig in {'stealth_rgbinfo', 'stealth_rgbcomp'}:
                        confirming_signature = False
                        sig_confirmed = True
                        reading_param_len = True
                        mode = 'rgb'
                        if decoded_sig == 'stealth_rgbcomp':
                            compressed = True
                        buffer_rgb = ''
                        index_rgb = 0
            elif reading_param_len:
                if mode == 'alpha':
                    if index_a == 32:
                        param_len = int(buffer_a, 2)
                        reading_param_len = False
                        reading_param = True
                        buffer_a = ''
                        index_a = 0
                else:
                    if index_rgb == 33:
                        pop = buffer_rgb[-1]
                        buffer_rgb = buffer_rgb[:-1]
                        param_len = int(buffer_rgb, 2)
                        reading_param_len = False
                        reading_param = True
                        buffer_rgb = pop
                        index_rgb = 1
            elif reading_param:
                if mode == 'alpha':
                    if index_a == param_len:
                        binary_data = buffer_a
                        read_end = True
                        break
                else:
                    if index_rgb >= param_len:
                        diff = param_len - index_rgb
                        if diff < 0:
                            buffer_rgb = buffer_rgb[:diff]
                        binary_data = buffer_rgb
                        read_end = True
                        break
            else:
                # impossible
                read_end = True
                break
        if read_end:
            break
    if not sig_confirmed or binary_data == "":
        return ""
        # Convert binary string to UTF-8 encoded text
    byte_data = bytearray(int(binary_data[i:i + 8], 2) for i in range(0, len(binary_data), 8))
    try:
        decoded_data = gzip.decompress(bytes(byte_data)).decode('utf-8')
    except Exception:
        decoded_data = byte_data.decode('utf-8', errors='ignore')
    return decoded_data


def add_stealth_pnginfo(image: PIL.Image.Image) -> Optional[PIL.Image.Image]:
    width, height = image.size
    image.putalpha(255)
    pixels = image.load()
    str_parameters = getattr(image, "text", None) and image.text.get("parameters")
    if not str_parameters:
        if getattr(image, "text", None) and "prompt" in image.text and "workflow" in image.text:
            str_parameters = json.dumps(image.text)
        else:
            return None
    # prepend signature
    signature_str = "stealth_pnginfo"

    binary_signature = "".join(format(byte, "08b") for byte in signature_str.encode("utf-8"))

    binary_param = "".join(format(byte, "08b") for byte in str_parameters.encode("utf-8"))

    # prepend length of parameters, padded to 32 digits
    param_len = len(binary_param)
    binary_param_len = format(param_len, "032b")

    binary_data = binary_signature + binary_param_len + binary_param
    index = 0
    for x in range(width):
        for y in range(height):
            if index < len(binary_data):
                r, g, b, a = pixels[x, y]

                # Modify the alpha value's least significant bit
                a = (a & ~1) | int(binary_data[index])

                pixels[x, y] = (r, g, b, a)
                index += 1
            else:
                break

    return image


def send_pillow_image_file(file, image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    buffer.seek(0)
    return flask.send_file(
        buffer,
        mimetype="image/png",
        as_attachment=True,
        download_name=f'{file.filename.split(".")[0]}_with_metadata.png',
    )


def download_file(url, file_path):
    with httpx.stream("GET", url) as response:
        with open(file_path, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=8192 * 16):
                f.write(chunk)


@app.route("/", methods=["POST"])
def load():
    files = [file for file in request.files.getlist("files") if file]
    urls_raw: str = request.form.get("urls", "")

    file_text_list = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        if urls_raw:
            urls_raw = urls_raw.replace("\t", " ").replace("\n", " ").replace(" ", " ").replace(",", " ")
            urls = [url for url in urls_raw.split(" ") if url]
            for url in urls:
                try:
                    file_path = os.path.join(tmp_dir, f"{uuid.uuid4()}")
                    download_file(url, file_path)
                    file = open(file_path, "rb")
                    file.filename = url
                    files.append(file)
                except Exception:
                    pass

        if not files:
            render_template("index.html", errors=[f"ERROR no files sent"])

        zip_file_path = os.path.join(tmp_dir, f"{uuid.uuid4()}.zip")
        with zipfile.ZipFile(zip_file_path, "w", compression=zipfile.ZIP_STORED) as zip_file:
            for file in files:
                image = PIL.Image.open(file)

                if request.form.get("checkbox"):
                    text_content = ""
                    if image.mode == "RGBA":
                        text_content = read_info_from_image_stealth(image)
                    if not text_content:
                        if getattr(image, "text", None):
                            text_content = image.text.get("parameters") or image.text
                        if not text_content:
                            text_content = "Metadata not found in alpha or pnginfo"
                    if text_content.startswith("{") and text_content.endswith("}"):
                        text_content_dict = json.loads(text_content)
                        text_content = "\n\n".join(f"{key}:\n{value}" for key, value in text_content_dict.items())
                    file_text_list.append(
                        dict(
                            text_content=text_content,
                            name=file.filename,
                        )
                    )
                    continue

                if image.mode == "RGBA":
                    new_metadata = read_info_from_image_stealth(image)
                    if new_metadata.startswith("{") and new_metadata.endswith("}"):
                        new_metadata = json.loads(new_metadata)
                    if isinstance(new_metadata, dict):
                        for key, value in new_metadata.items():
                            image.text[key] = value
                    else:
                        image.text["parameters"] = new_metadata

                    if request.form.get("checkbox"):
                        if isinstance(new_metadata, dict):
                            new_metadata = "\n\n".join(f"{key}:\n{value}" for key, value in new_metadata.items())

                        file_text_list.append(
                            dict(
                                text_content=new_metadata,
                                name=file.filename,
                            )
                        )
                        continue
                    if len(files) == 1:
                        return send_pillow_image_file(file, image)
                    else:
                        buffer = io.BytesIO()
                        image.save(buffer, format="PNG", optimize=False)
                        buffer.seek(0)
                        zip_file.writestr(
                            f'{file.filename.split(".")[0]}_with_metadata.png',
                            buffer.read(),
                        )
                else:
                    image = add_stealth_pnginfo(image)
                    if not image:
                        return render_template(
                            "index.html",
                            errors=[f"ERROR one of the images was missing metadata {file.filename}"],
                        )
                    if not image.text:
                        return render_template(
                            "index.html",
                            errors=[f"Failed to get original text from {file.filename}."],
                        )
                    if len(files) == 1:
                        return send_pillow_image_file(file, image)
                    else:
                        buffer = io.BytesIO()
                        image.save(buffer, format="PNG", optimize=False)
                        buffer.seek(0)
                        zip_file.writestr(
                            f'{file.filename.split(".")[0]}_with_metadata.png',
                            buffer.read(),
                        )
        if request.form.get("checkbox"):
            return render_template("index.html", file_list=enumerate(file_text_list))
        else:
            return flask.send_file(
                zip_file_path,
                mimetype="application/zip",
                as_attachment=True,
                download_name=f"all_files_processed.zip",
            )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT") or 7090),
        debug=os.getenv("DEBUG") == "True",
    )
