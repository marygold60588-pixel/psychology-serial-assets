#!/usr/bin/env python3
"""Burn psychological captions onto an existing final.mp4.

Reads episodes/<id>/caption-request.json. Writes only final-captioned.mp4.
Does not modify final.mp4, assemble-request.json, or requested_at.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

FONT_FILE = os.environ.get(
    "CAPTION_FONT",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
FONT_NAME = os.environ.get("CAPTION_FONT_NAME", "DejaVu Sans")
POSITIONS = {
    "hook": (540, 1420),
    "body": (540, 1460),
    "foot": (540, 1660),
}


def fail(message):
    print(message, file=sys.stderr)
    return 1


def ass_time(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    rest = seconds - hours * 3600 - minutes * 60
    return "%d:%02d:%05.2f" % (hours, minutes, rest)


def ass_text(value):
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if len(lines) > 2:
        raise ValueError("a caption has more than 2 lines")
    escaped = []
    for line in lines:
        line = line.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        escaped.append(line)
    return "\\N".join(escaped)


def write_ass(cues, destination):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,%(font)s,46,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,5,90,90,0,1
Style: Body,%(font)s,40,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,5,100,100,0,1
Style: Foot,%(font)s,28,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,2,1,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" % {"font": FONT_NAME}
    style_names = {"hook": "Hook", "body": "Body", "foot": "Foot"}
    lines = [header]
    for cue in cues:
        style = cue["style"]
        x, y = POSITIONS[style]
        text = ass_text(cue["text"])
        override = "{\\fad(150,200)\\an5\\pos(%d,%d)}" % (x, y)
        lines.append(
            "Dialogue: 0,%s,%s,%s,,0,0,0,,%s%s\n"
            % (ass_time(cue["start"]), ass_time(cue["end"]), style_names[style], override, text)
        )
    with open(destination, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(lines))


def probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name",
            "-show_entries", "format=duration",
            "-of", "json",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    duration = float(payload["format"]["duration"])
    return int(stream["width"]), int(stream["height"]), stream["codec_name"], duration


def main(argv):
    if len(argv) != 2:
        return fail("usage: caption_episode.py episodes/<id>/caption-request.json")
    request_path = argv[1]
    with open(request_path, "r", encoding="utf-8") as handle:
        request = json.load(handle)
    episode_id = str(request.get("episode_id") or "")
    source = str(request.get("source") or "")
    output = str(request.get("output") or "")
    cues = request.get("cues")
    folder = "episodes/" + episode_id
    if not episode_id or os.path.normpath(os.path.dirname(request_path)).replace("\\", "/") != folder:
        return fail("caption request path does not match episode_id")
    if source != folder + "/final.mp4":
        return fail("source must be the existing final.mp4")
    if output != folder + "/final-captioned.mp4":
        return fail("output must be final-captioned.mp4")
    if not isinstance(cues, list) or not cues:
        return fail("caption request has no cues")
    if not os.path.isfile(source):
        return fail("existing final.mp4 is missing")
    if not os.path.isfile(FONT_FILE):
        return fail("caption font is missing: " + FONT_FILE)
    for cue in cues:
        if cue.get("style") not in POSITIONS:
            return fail("unknown caption style")
        if float(cue["end"]) <= float(cue["start"]):
            return fail("caption timing is empty")
        ass_text(cue.get("text") or "")
    width, height, codec, duration = probe(source)
    if width != 1080 or height != 1920 or codec != "h264":
        return fail("source is not 1080x1920 h264")
    if duration < 24 or duration > 27:
        return fail("source duration is unexpected: %.3f" % duration)
    work = tempfile.mkdtemp(prefix="caption-")
    try:
        ass_file = os.path.join(work, "captions.ass")
        staged = os.path.join(work, "final-captioned.mp4")
        shutil.copy(FONT_FILE, os.path.join(work, os.path.basename(FONT_FILE)))
        write_ass(cues, ass_file)
        command = [
            "ffmpeg", "-y", "-i", os.path.abspath(source),
            "-vf", "ass=captions.ass:fontsdir=.",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", "30", "-an",
            "-movflags", "+faststart",
            "final-captioned.mp4",
        ]
        subprocess.run(command, cwd=work, check=True)
        out_width, out_height, out_codec, out_duration = probe(staged)
        print("width=%d" % out_width)
        print("height=%d" % out_height)
        print("duration=%.3f" % out_duration)
        print("codec=%s" % out_codec)
        if out_width != 1080 or out_height != 1920 or out_codec != "h264":
            return fail("captioned video has the wrong format")
        if abs(out_duration - duration) > 0.2:
            return fail("captioned duration changed the source")
        os.makedirs(os.path.dirname(output), exist_ok=True)
        shutil.move(staged, output)
        if not os.path.isfile(source):
            return fail("source final.mp4 disappeared")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
