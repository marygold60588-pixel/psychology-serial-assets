#!/usr/bin/env python3
"""Add a licensed music bed to an existing captioned video.

Reads episodes/<id>/music-request.json and music-license.json.
Writes only final-captioned-music.mp4.
Does not modify final.mp4 or final-captioned.mp4.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

EPISODE_RE = re.compile(r"^episodes/([0-9a-fA-F-]{36})/")


def fail(message):
    print(message, file=sys.stderr)
    return 1


def run(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail[-2000:] or "command failed")
    return result


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path):
    result = run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,channels,sample_rate",
        "-of", "json", path,
    ])
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "video_codec": video.get("codec_name") or "",
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "audio_codec": audio.get("codec_name") or "",
        "sample_rate": audio.get("sample_rate") or "",
        "channels": int(audio.get("channels") or 0),
    }


def download(url, dest):
    request = urllib.request.Request(url, headers={"User-Agent": "psychology-serial-music/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        kind = response.headers.get("Content-Type", "")
        if "audio" not in kind and "octet-stream" not in kind:
            raise RuntimeError("music URL did not return audio")
        with open(dest, "wb") as handle:
            shutil.copyfileobj(response, handle)


def loudnorm_measurement(music, duration):
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", music,
        "-af", "atrim=0:%s,loudnorm=I=-24:TP=-1.5:LRA=11:print_format=json" % duration,
        "-f", "null", "-",
    ], text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "loudnorm measure failed")[-2000:])
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr, re.S)
    if not match:
        raise RuntimeError("loudnorm did not print measurement")
    data = json.loads(match.group(0))
    return (
        "loudnorm=I=-24:TP=-1.5:LRA=11:"
        "measured_I=%s:measured_LRA=%s:measured_TP=%s:measured_thresh=%s:offset=%s:linear=true"
        % (
            data["input_i"],
            data["input_lra"],
            data["input_tp"],
            data["input_thresh"],
            data["target_offset"],
        )
    )


def integrated_loudness(path):
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-",
    ], text=True, capture_output=True)
    text = result.stderr or ""
    summary = text.split("Summary:")[-1]
    integrated = re.search(r"I:\s*(-?\d+(?:\.\d+)?)\s+LUFS", summary)
    peak = re.search(r"(?:Peak|True peak):\s*(-?\d+(?:\.\d+)?)\s+dBFS", summary)
    if result.returncode != 0 or not integrated or not peak:
        raise RuntimeError("could not measure output loudness")
    return float(integrated.group(1)), float(peak.group(1))


def main(argv):
    if len(argv) != 2:
        return fail("usage: add_music.py episodes/<id>/music-request.json")
    request_path = argv[1].replace("\\", "/")
    match = EPISODE_RE.match(request_path)
    if not match or not request_path.endswith("/music-request.json"):
        return fail("request must be episodes/<id>/music-request.json")
    episode_id = match.group(1)
    folder = "episodes/" + episode_id
    with open(request_path, encoding="utf-8") as handle:
        request = json.load(handle)
    if request.get("episode_id") != episode_id:
        return fail("episode_id does not match the request path")
    source = str(request.get("source") or "").replace("\\", "/")
    output = str(request.get("output") or "").replace("\\", "/")
    license_path = str(request.get("license_file") or "").replace("\\", "/")
    if source != folder + "/final-captioned.mp4":
        return fail("source must be the existing final-captioned.mp4")
    if output != folder + "/final-captioned-music.mp4":
        return fail("output must be final-captioned-music.mp4")
    if license_path != folder + "/music-license.json":
        return fail("license file must be music-license.json")
    if not os.path.isfile(source):
        return fail("captioned video is missing")
    with open(license_path, encoding="utf-8") as handle:
        license_info = json.load(handle)
    title = str(license_info.get("title") or "").strip()
    artist = str(license_info.get("artist") or "").strip()
    music_url = str(license_info.get("source_url") or "").strip()
    license_name = str(license_info.get("license_name") or "").strip()
    if not title or not artist:
        return fail("music license is missing title or artist")
    if license_name != "Mixkit Stock Music Free License":
        return fail("music license is not the Mixkit Stock Music Free License")
    if not music_url.startswith("https://assets.mixkit.co/music/") or not music_url.endswith(".mp3"):
        return fail("music URL must be an assets.mixkit.co mp3")
    before = sha256(source)
    info = probe(source)
    if info["width"] != 1080 or info["height"] != 1920 or info["video_codec"] != "h264":
        return fail("source video must stay 1080x1920 h264")
    if info["duration"] < 24 or info["duration"] > 27:
        return fail("source duration is outside the finished episode length")
    duration = "%.6f" % info["duration"]
    fade_out = info["duration"] - 1.0
    work = tempfile.mkdtemp(prefix="music-bed-")
    try:
        music = os.path.join(work, "music.mp3")
        staged = os.path.join(work, "final-captioned-music.mp4")
        download(music_url, music)
        music_info = probe(music)
        if not music_info["audio_codec"] or music_info["duration"] < info["duration"]:
            return fail("downloaded music is shorter than the video")
        norm = loudnorm_measurement(music, duration)
        audio_filter = (
            "[1:a]atrim=0:%s,asetpts=PTS-STARTPTS,%s,"
            "afade=t=in:st=0:d=0.5,afade=t=out:st=%.6f:d=1,"
            "atrim=0:%s,asetpts=PTS-STARTPTS[a]"
        ) % (duration, norm, fade_out, duration)
        run([
            "ffmpeg", "-y", "-i", os.path.abspath(source), "-i", music,
            "-filter_complex", audio_filter,
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", staged,
        ])
        out = probe(staged)
        if out["width"] != 1080 or out["height"] != 1920 or out["video_codec"] != "h264":
            return fail("music mix changed the picture")
        if out["audio_codec"] != "aac":
            return fail("music mix did not create AAC audio")
        if abs(out["duration"] - info["duration"]) > 0.05:
            return fail("music mix changed the duration")
        loudness, peak = integrated_loudness(staged)
        if loudness < -28 or loudness > -20:
            return fail("music loudness %s LUFS is outside the quiet bed range" % loudness)
        if peak >= -0.1:
            return fail("music peak %s dBFS clips" % peak)
        if sha256(source) != before or not os.path.isfile(source):
            return fail("source video changed")
        os.replace(staged, output)
        print("title=%s" % title)
        print("artist=%s" % artist)
        print("width=%s" % out["width"])
        print("height=%s" % out["height"])
        print("duration=%.6f" % out["duration"])
        print("video_codec=%s" % out["video_codec"])
        print("audio_codec=%s" % out["audio_codec"])
        print("loudness=%.1f" % loudness)
        print("peak=%.1f" % peak)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:
        sys.exit(fail(str(exc)))
