#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mc_auto_piano —— 21 键 Minecraft 自动弹琴脚本

键位布局::

            1   2   3   4   5   6   7
    高音    Q   W   E   R   T   Y   U
    中音    A   S   D   F   G   H   J
    低音    Z   X   C   V   B   N   M

用法::

    python mc_piano.py scores/小星星.txt
    python mc_piano.py song.mid --track 0
    python mc_piano.py "1 2 3 1 | 3 2 1 -" --bpm 100
    python mc_piano.py scores/小星星.txt --dry-run
    python mc_piano.py --list-keys

运行后会有倒计时，此时切到游戏窗口保持焦点。中途按 F8 立即停止。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Sequence

# --------------------------------------------------------------------------- #
#  键位表
# --------------------------------------------------------------------------- #

OCTAVE_KEYS: dict[str, str] = {
    "high": "qwertyu",
    "mid": "asdfghj",
    "low": "zxcvbnm",
}
OCTAVE_LABEL: dict[str, str] = {"high": "高", "mid": "中", "low": "低"}
OCTAVE_INDEX: dict[str, int] = {"low": -1, "mid": 0, "high": 1}
INDEX_OCTAVE: dict[int, str] = {-1: "low", 0: "mid", 1: "high"}

# 简谱级数 -> 相对主音的半音数（1=do ... 7=si）
DEGREE_SEMITONE: dict[int, int] = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11}

# 12 个半音各自映射到最接近的自然音级（本琴没有半音键，遇升降号只能就近吸附）
_SEMI_DOWN: tuple[int, ...] = (1, 1, 2, 2, 3, 4, 4, 5, 5, 6, 6, 7)  # 往下靠
_SEMI_UP: tuple[int, ...] = (1, 2, 2, 3, 3, 4, 5, 5, 6, 6, 7, 7)     # 往上靠

ALL_KEYS: str = "".join(OCTAVE_KEYS[o] for o in ("high", "mid", "low"))

ABORT_VK = 0x77  # F8


def key_for(octave_index: int, degree: int) -> str:
    """(八度序号 -1/0/1, 音级 1-7) -> 键位字母。"""
    return OCTAVE_KEYS[INDEX_OCTAVE[octave_index]][degree - 1]


def clamp_octave(octave_index: int) -> int:
    return -1 if octave_index < -1 else (1 if octave_index > 1 else octave_index)


def midi_to_position(midi: int, base: int = 60, snap: str = "down") -> tuple[int, int]:
    """MIDI 音高 -> (八度序号, 音级)。base 是「中音 1」对应的 MIDI 号。"""
    table = _SEMI_UP if snap == "up" else _SEMI_DOWN
    return midi // 12 - base // 12, table[midi % 12]


def position_to_midi(octave_index: int, degree: int, base: int = 60) -> int:
    return base + octave_index * 12 + DEGREE_SEMITONE[degree]


# --------------------------------------------------------------------------- #
#  数据结构
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Note:
    """一个待按下的键。"""

    time: float          # 起始时间（秒，相对曲首）
    key: str             # 键位字母
    duration: float      # 原始时值（秒，未乘 gate）
    name: str = ""       # 显示用音名，如 中5 / 高1
    gate: float = 0.9    # 按下占时值的比例


@dataclass(slots=True)
class Action:
    time: float
    key: str
    down: bool
    name: str = ""


# --------------------------------------------------------------------------- #
#  简谱文本解析
# --------------------------------------------------------------------------- #

_DIRECTIVE_RE = re.compile(r"\s*@(\w+)\s*[=: ]\s*(.*?)\s*$")
_OCTAVE_WORDS = {
    "h": "high", "high": "high", "^": "high", "高": "high", "高音": "high",
    "m": "mid", "mid": "mid", "中": "mid", "中音": "mid",
    "l": "low", "low": "low", "_": "low", "低": "low", "低音": "low",
}


def _parse_beats(text: str) -> float:
    """'2' / '0.5' / '1/2' -> 拍数。"""
    text = text.strip()
    if not text:
        return 1.0
    if "/" in text:
        num, _, den = text.partition("/")
        value = float(num) / float(den)
    else:
        value = float(text)
    if value <= 0:
        raise ValueError("拍数必须为正")
    return value


def _parse_note_token(token: str, default_octave: str) -> tuple[str, int, str]:
    """'^5' / '_3' / 'h2' / '4#' -> (八度名, 音级, 升降号)"""
    octave = default_octave
    if token and token[0] in "^_":
        octave = "high" if token[0] == "^" else "low"
        token = token[1:]
    elif len(token) >= 2 and token[0] in "hmlHML" and token[1].isdigit():
        octave = {"h": "high", "m": "mid", "l": "low"}[token[0].lower()]
        token = token[1:]

    accidental = ""
    if token and token[-1] in "#b":
        accidental, token = token[-1], token[:-1]

    if len(token) != 1 or token not in "1234567":
        raise ValueError(f"无法识别的音符 {token!r}")
    return octave, int(token), accidental


def parse_text_score(
    text: str,
    *,
    bpm: float = 120.0,
    gate: float = 0.9,
    default_octave: str = "mid",
    base: int = 60,
    transpose: int = 0,
    snap: str = "down",
) -> tuple[list[Note], dict, list[str]]:
    """解析简谱文本，返回 (音符列表, 元信息, 警告列表)。

    记谱法::

        1-7        音级（中音，默认）
        ^1 / _1    高音 / 低音（也可写 h1 / l1）
        1:2        时值 2 拍，支持小数 0.5 与分数 1/2
        -          延音线，把前一个音延长一拍
        0          休止符
        1+3+5      和弦，同时按下
        |          小节线，纯装饰，会被忽略
        //  ;  #  注释
        @bpm=120   行首指令：@bpm @beat @gate @octave @title
    """
    sec_per_beat = 60.0 / float(bpm)
    default_oct = default_octave
    current_gate = float(gate)

    text = text.lstrip("\ufeff")  # 去掉可能存在的 BOM（记事本另存为 UTF-8 会加）

    notes: list[Note] = []
    warnings: list[str] = []
    meta: dict = {"title": "", "bpm": bpm}
    cursor = 0.0
    pending: list[Note] = []
    accidentals: set[str] = set()

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw
        for marker in ("//", ";"):
            cut = line.find(marker)
            if cut != -1:
                line = line[:cut]
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        directive = _DIRECTIVE_RE.match(line)
        if directive:
            name = directive.group(1).lower()
            value = directive.group(2).strip()
            try:
                if name in ("bpm", "tempo"):
                    bpm = float(value)
                    sec_per_beat = 60.0 / bpm
                    meta["bpm"] = bpm
                elif name in ("beat", "sec_per_beat"):
                    sec_per_beat = float(value)
                elif name == "gate":
                    current_gate = float(value)
                elif name in ("octave", "oct", "o"):
                    key = value.lower()
                    if key in _OCTAVE_WORDS:
                        default_oct = _OCTAVE_WORDS[key]
                    else:
                        warnings.append(f"第 {lineno} 行：未知八度 {value!r}，已忽略")
                elif name in ("title", "name"):
                    meta["title"] = value
                else:
                    warnings.append(f"第 {lineno} 行：未知指令 @{name}，已忽略")
            except ValueError:
                warnings.append(f"第 {lineno} 行：@{name} 的值 {value!r} 不合法，已忽略")
            continue

        # 允许 1 + 3 + 5、1 : 2 这类带空格的写法
        line = re.sub(r"\s*([+:])\s*", r"\1", line)

        for token in line.split():
            if token == "|" or token == "":
                continue

            if token == "-":
                if pending:
                    for note in pending:
                        note.duration += sec_per_beat
                    cursor += sec_per_beat
                else:
                    warnings.append(f"第 {lineno} 行：'-' 前面没有音符，已忽略")
                continue

            body, sep, dur_text = token.partition(":")
            try:
                duration = (_parse_beats(dur_text) if sep else 1.0) * sec_per_beat
            except ValueError:
                warnings.append(f"第 {lineno} 行：时值 {dur_text!r} 不合法，已忽略 {token!r}")
                continue

            if body in ("0", "r", "R", "休"):
                pending = []
                cursor += duration
                continue

            try:
                parsed = [_parse_note_token(p, default_oct) for p in body.split("+") if p]
                if not parsed:
                    raise ValueError("空音符")
            except ValueError:
                warnings.append(f"第 {lineno} 行：无法识别 {token!r}，已忽略")
                pending = []
                continue

            group: list[Note] = []
            seen: set[str] = set()
            for octave, degree, accidental in parsed:
                if accidental:
                    accidentals.add(accidental)
                    if accidental == "#":
                        degree += 1
                        if degree > 7:
                            degree = 1
                            octave = INDEX_OCTAVE[clamp_octave(OCTAVE_INDEX[octave] + 1)]
                    else:
                        degree -= 1
                        if degree < 1:
                            degree = 7
                            octave = INDEX_OCTAVE[clamp_octave(OCTAVE_INDEX[octave] - 1)]

                midi = position_to_midi(OCTAVE_INDEX[octave], degree, base) + transpose
                octave_index, mapped_degree = midi_to_position(midi, base, snap)
                octave_index = clamp_octave(octave_index)

                key = key_for(octave_index, mapped_degree)
                if key in seen:
                    continue
                seen.add(key)
                group.append(
                    Note(
                        time=cursor,
                        key=key,
                        duration=duration,
                        name=f"{OCTAVE_LABEL[INDEX_OCTAVE[octave_index]]}{mapped_degree}",
                        gate=current_gate,
                    )
                )

            notes.extend(group)
            pending = group
            cursor += duration

    if accidentals:
        warnings.append(
            f"谱面含升降号 {' '.join(sorted(accidentals))}，21 键琴没有半音键，"
            f"已就近吸附到自然音级（--snap 可切换方向）"
        )

    meta["duration"] = max((n.time + n.duration for n in notes), default=0.0)
    return notes, meta, warnings


# --------------------------------------------------------------------------- #
#  MIDI 解析
# --------------------------------------------------------------------------- #


def _read_vlq(data: bytes, pos: int) -> tuple[int, int]:
    """读取 MIDI 的可变长度量。"""
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise ValueError("MIDI 数据在可变长度量处意外结束")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise ValueError("MIDI 可变长度量超过 4 字节")


@dataclass(slots=True)
class _TrackData:
    events: list[tuple[int, int, int, int]]  # (tick, status 完整字节含通道, data1, data2)
    tempos: list[tuple[int, int]]            # (tick, 微秒/拍)
    name: str = ""


def _decode_track_name(payload: bytes) -> str:
    """MIDI 轨道名没有规定编码，UTF-8 试不出来就退回 GBK / Latin-1。"""
    for encoding in ("utf-8", "gbk", "big5", "latin-1"):
        try:
            return payload.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", "replace").strip()


def _decode_track(body: bytes) -> _TrackData:
    """把一条 MTrk 解码成事件列表（时间单位仍是 tick）。"""
    events: list[tuple[int, int, int, int]] = []
    tempos: list[tuple[int, int]] = []
    name = ""
    tick = 0
    pos = 0
    running: int | None = None
    size = len(body)

    while pos < size:
        delta, pos = _read_vlq(body, pos)
        tick += delta
        if pos >= size:
            break

        byte = body[pos]
        if byte & 0x80:
            status = byte
            pos += 1
            if status < 0xF0:
                running = status
        else:
            if running is None:
                raise ValueError("MIDI 数据损坏：出现 running status 但没有前导状态字节")
            status = running

        if status == 0xFF:  # meta 事件
            if pos >= size:
                break
            meta_type = body[pos]
            pos += 1
            length, pos = _read_vlq(body, pos)
            payload = body[pos:pos + length]
            pos += length
            if meta_type == 0x51 and length == 3:
                tempos.append((tick, int.from_bytes(payload, "big")))
            elif meta_type == 0x03 and not name:
                name = _decode_track_name(payload)
            elif meta_type == 0x2F:  # end of track
                break
            continue

        if status in (0xF0, 0xF7):  # sysex
            length, pos = _read_vlq(body, pos)
            pos += length
            continue

        high = status & 0xF0
        width = 1 if high in (0xC0, 0xD0) else 2
        if pos + width > size:
            break
        data1 = body[pos]
        data2 = body[pos + 1] if width == 2 else 0
        pos += width
        events.append((tick, status, data1, data2))

    return _TrackData(events=events, tempos=tempos, name=name)


def _read_smf(path: str) -> tuple[list[_TrackData], int, str]:
    with open(path, "rb") as handle:
        blob = handle.read()

    if len(blob) < 14 or blob[0:4] != b"MThd":
        raise ValueError("不是标准 MIDI 文件（缺少 MThd 头）")

    header_length = int.from_bytes(blob[4:8], "big")
    ticks_per_beat = int.from_bytes(blob[12:14], "big")
    if ticks_per_beat & 0x8000:
        raise ValueError("不支持 SMPTE 时间格式的 MIDI 文件，请用 --midi-engine mido")

    pos = 8 + header_length
    tracks: list[_TrackData] = []
    while pos + 8 <= len(blob):
        chunk_id = blob[pos:pos + 4]
        chunk_length = int.from_bytes(blob[pos + 4:pos + 8], "big")
        body = blob[pos + 8:pos + 8 + chunk_length]
        pos += 8 + chunk_length
        if chunk_id == b"MTrk":
            tracks.append(_decode_track(body))

    if not tracks:
        raise ValueError("MIDI 文件里没有找到任何 MTrk 轨道")
    return tracks, (ticks_per_beat or 480), (tracks[0].name if tracks else "")


def _tempo_converter(tempos: Sequence[tuple[int, int]], ticks_per_beat: int) -> Callable[[int], float]:
    """返回 tick -> 秒 的换算函数。"""
    table = sorted({0: 500_000, **dict(tempos)}.items())

    def convert(tick: int) -> float:
        seconds = 0.0
        last_tick = 0
        last_tempo = 500_000
        for moment, tempo in table:
            if moment >= tick:
                break
            seconds += (moment - last_tick) * last_tempo / 1_000_000.0 / ticks_per_beat
            last_tick, last_tempo = moment, tempo
        seconds += (tick - last_tick) * last_tempo / 1_000_000.0 / ticks_per_beat
        return seconds

    return convert


def _midi_notes_builtin(path: str, track: int | None) -> tuple[list[tuple[float, int, float]], str, float]:
    tracks, ticks_per_beat, title = _read_smf(path)

    if track is not None:
        if not 0 <= track < len(tracks):
            raise SystemExit(f"--track {track} 超出范围，该文件有 {len(tracks)} 条轨道")
        chosen = [tracks[track]]
    else:
        chosen = tracks

    all_tempos = [item for t in tracks for item in t.tempos]
    convert = _tempo_converter(all_tempos, ticks_per_beat)

    raw: list[tuple[float, int, float]] = []
    for item in chosen:
        active: dict[tuple[int, int], int] = {}
        for tick, status, note, velocity in item.events:
            high = status & 0xF0
            slot = (status & 0x0F, note)
            if high == 0x90 and velocity > 0:
                active[slot] = tick
            elif high in (0x80, 0x90):
                start_tick = active.pop(slot, None)
                if start_tick is not None:
                    start = convert(start_tick)
                    raw.append((start, note, convert(tick) - start))

    raw.sort(key=lambda entry: (entry[0], entry[1]))
    first_tempo = min((t for t in all_tempos), default=(0, 500_000))[1]
    return raw, title, 60_000_000.0 / first_tempo


def _midi_notes_mido(path: str, track: int | None) -> tuple[list[tuple[float, int, float]], str, float]:
    try:
        import mido
    except ImportError as exc:
        raise SystemExit("--midi-engine mido 需要先安装：pip install mido") from exc

    mid = mido.MidiFile(path)
    if track is not None:
        if not 0 <= track < len(mid.tracks):
            raise SystemExit(f"--track {track} 超出范围，该文件有 {len(mid.tracks)} 条轨道")
        tracks = [mid.tracks[track]]
    else:
        tracks = list(mid.tracks)

    tempo = 500_000
    first_tempo = 500_000
    seen_tempo = False
    now = 0.0
    active: dict[tuple[int, int], float] = {}
    raw: list[tuple[float, int, float]] = []

    for msg in mido.merge_tracks(tracks):
        now += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo = msg.tempo
            if not seen_tempo:
                first_tempo, seen_tempo = tempo, True
        elif msg.type == "note_on" and msg.velocity > 0:
            active[(msg.channel, msg.note)] = now
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            started = active.pop((msg.channel, msg.note), None)
            if started is not None:
                raw.append((started, msg.note, now - started))

    raw.sort(key=lambda entry: (entry[0], entry[1]))
    return raw, "", 60_000_000.0 / first_tempo


def parse_midi_score(
    path: str,
    *,
    track: int | None = None,
    base: int = 60,
    transpose: int = 0,
    snap: str = "down",
    gate: float = 0.9,
    quantize: float = 0.0,
    engine: str = "builtin",
) -> tuple[list[Note], dict, list[str]]:
    """读取 .mid，转成待弹奏的音符列表。默认用内置解析器，不需要第三方库。"""
    if engine == "mido":
        raw_notes, track_title, bpm = _midi_notes_mido(path, track)
    else:
        try:
            raw_notes, track_title, bpm = _midi_notes_builtin(path, track)
        except ValueError as exc:
            raise SystemExit(f"解析 MIDI 失败：{exc}") from exc

    warnings: list[str] = []
    notes: list[Note] = []
    duplicates = 0
    out_of_range = 0
    seen: set[tuple[float, str]] = set()

    for start, midi_note, length in raw_notes:
        if quantize > 0:
            start = round(start / quantize) * quantize
        octave_index, degree = midi_to_position(midi_note + transpose, base, snap)
        if not -1 <= octave_index <= 1:
            out_of_range += 1
        octave_index = clamp_octave(octave_index)
        key = key_for(octave_index, degree)
        pair = (round(start, 6), key)
        if pair in seen:
            duplicates += 1
            continue
        seen.add(pair)
        notes.append(
            Note(
                time=start,
                key=key,
                duration=max(length, 0.0),
                name=f"{OCTAVE_LABEL[INDEX_OCTAVE[octave_index]]}{degree}",
                gate=gate,
            )
        )

    if out_of_range:
        warnings.append(
            f"有 {out_of_range} 个音超出 3 个八度范围，已整体移调对齐；"
            f"可用 --transpose 手动调整"
        )
    if duplicates:
        warnings.append(f"忽略了 {duplicates} 个同键同刻的重复音")

    meta = {
        "title": track_title or os.path.basename(path),
        "bpm": round(bpm, 2),
        "duration": max((n.time + n.duration for n in notes), default=0.0),
    }
    return notes, meta, warnings


# --------------------------------------------------------------------------- #
#  时间轴整理
# --------------------------------------------------------------------------- #


def build_timeline(
    notes: Sequence[Note],
    *,
    min_hold: float = 0.035,
    min_gap: float = 0.012,
    speed: float = 1.0,
) -> list[Action]:
    """把音符整理成 (按下/抬起) 动作序列。

    - 按 gate 缩短时值
    - 保证同一个键两次按下之间至少留出 min_gap
    - 保证单次按下不短于 min_hold（除非会撞上下一个音）
    """
    if speed <= 0:
        raise SystemExit("--speed 必须大于 0")

    per_key: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        per_key[note.key].append(note)

    actions: list[Action] = []
    for key, items in per_key.items():
        items.sort(key=lambda n: n.time)
        for index, note in enumerate(items):
            start = note.time / speed
            length = max(note.duration * note.gate, 0.0) / speed
            next_start = items[index + 1].time / speed if index + 1 < len(items) else None

            latest_end = next_start - min_gap if next_start is not None else float("inf")
            if length < min_hold:
                length = min(min_hold, latest_end - start) if latest_end != float("inf") else min_hold
            if start + length > latest_end:
                length = latest_end - start
            if length <= 0.001:
                continue

            actions.append(Action(start, key, True, note.name))
            actions.append(Action(start + length, key, False, note.name))

    actions.sort(key=lambda a: (a.time, a.down))
    return actions


# --------------------------------------------------------------------------- #
#  Windows 窗口 / 权限辅助
# --------------------------------------------------------------------------- #

IS_WINDOWS = os.name == "nt"


def _user32():
    import ctypes

    return ctypes.WinDLL("user32", use_last_error=True)


def _kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def get_window_title(hwnd: int) -> str:
    import ctypes

    user32 = _user32()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_window_pid(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_process_exe(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def is_process_elevated(pid: int) -> bool | None:
    """目标进程是否以管理员运行。返回 None 表示查不到（通常意味着对方权限比我们高）。"""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TOKEN_ELEVATION = 20

    class TOKEN_ELEVATION_INFO(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    kernel32, advapi32 = _kernel32(), ctypes.WinDLL("advapi32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            info = TOKEN_ELEVATION_INFO()
            returned = wintypes.DWORD()
            ok = advapi32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(info),
                ctypes.sizeof(info), ctypes.byref(returned),
            )
            return bool(info.TokenIsElevated) if ok else None
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def is_self_elevated() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def get_foreground_window() -> int:
    if not IS_WINDOWS:
        return 0
    try:
        return int(_user32().GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return 0


def needs_elevation(hwnd: int) -> bool:
    """目标窗口权限比本进程高吗？（高的话按键会被 UIPI 静默丢弃）"""
    if not IS_WINDOWS or not hwnd or is_self_elevated():
        return False
    return is_process_elevated(get_window_pid(hwnd)) is True


def relaunch_elevated(extra_argv: Sequence[str]) -> bool:
    """用管理员身份重启自己。成功则本进程应当退出。"""
    if not IS_WINDOWS:
        return False
    import ctypes
    import subprocess

    script = os.path.abspath(__file__)
    params = subprocess.list2cmdline([script, *extra_argv])
    SW_SHOWNORMAL = 1
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, os.getcwd(), SW_SHOWNORMAL
        )
    except Exception:  # noqa: BLE001
        return False
    return int(result) > 32


def list_windows() -> list[tuple[int, str, str]]:
    """所有可见的顶层窗口：(hwnd, 标题, 进程名)。"""
    import ctypes
    from ctypes import wintypes

    if not IS_WINDOWS:
        return []

    user32 = _user32()
    results: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = get_window_title(hwnd)
        if not title:
            return True
        results.append((int(hwnd), title, get_process_exe(get_window_pid(hwnd))))
        return True

    user32.EnumWindows(callback, 0)
    return results


def find_window(keyword: str) -> int:
    """按标题子串（不区分大小写）找窗口，找不到返回 0。"""
    keyword = keyword.lower()
    for hwnd, title, exe in list_windows():
        if keyword in title.lower() or keyword in exe.lower():
            return hwnd
    return 0


def focus_window(hwnd: int) -> bool:
    """把窗口切到前台。用 AttachThreadInput 绕过 Windows 的前台锁定。"""
    if not IS_WINDOWS or not hwnd:
        return False
    import ctypes

    user32 = _user32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    else:
        user32.ShowWindow(hwnd, 5)  # SW_SHOW

    if user32.GetForegroundWindow() == hwnd:
        return True

    kernel32 = _kernel32()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = kernel32.GetCurrentThreadId()
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

    attached = []
    try:
        for thread in (foreground_thread, target_thread):
            if thread and thread != current_thread:
                if user32.AttachThreadInput(current_thread, thread, True):
                    attached.append(thread)
        user32.BringWindowToTop(hwnd)
        result = bool(user32.SetForegroundWindow(hwnd))
    finally:
        for thread in attached:
            user32.AttachThreadInput(current_thread, thread, False)

    return result and user32.GetForegroundWindow() == hwnd


# --------------------------------------------------------------------------- #
#  按键后端
# --------------------------------------------------------------------------- #


class Backend:
    name = "base"

    def key_down(self, key: str) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    def key_up(self, key: str) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    def release_all(self) -> None:  # pragma: no cover - 接口
        pass

    def close(self) -> None:
        pass


class SendInputBackend(Backend):
    """纯 ctypes 的 SendInput（扫描码），零第三方依赖，效果等同 pydirectinput。"""

    name = "sendinput"
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    INPUT_KEYBOARD = 1

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        if os.name != "nt":
            raise RuntimeError("SendInput 后端仅支持 Windows")

        self._ctypes = ctypes
        self._held: set[str] = set()

        ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        self._INPUT = INPUT
        self._INPUTU = _INPUTUNION
        self._KEYBDINPUT = KEYBDINPUT
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._send_input = self._user32.SendInput
        self._send_input.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self._send_input.restype = wintypes.UINT

        self._scancodes: dict[str, int] = {}
        for char in ALL_KEYS:
            # A-Z 的虚拟键码就是其 ASCII 码，直接用，不依赖键盘布局
            vk = ord(char.upper())
            self._scancodes[char] = self._user32.MapVirtualKeyW(vk, 0)

        self.blocked = False

    def _emit(self, key: str, up: bool) -> None:
        flags = self.KEYEVENTF_SCANCODE | (self.KEYEVENTF_KEYUP if up else 0)
        event = self._INPUT(
            type=self.INPUT_KEYBOARD,
            u=self._INPUTU(
                ki=self._KEYBDINPUT(
                    wVk=0, wScan=self._scancodes[key], dwFlags=flags, time=0, dwExtraInfo=0
                )
            ),
        )
        self._ctypes.set_last_error(0)
        sent = self._send_input(1, self._ctypes.byref(event), self._ctypes.sizeof(event))
        if sent != 1 and not self.blocked:
            # SendInput 返回 0 基本只有一个原因：UIPI 拦下了（目标窗口权限比我们高）
            self.blocked = True
            err = self._ctypes.get_last_error()
            hint = "目标窗口以管理员身份运行" if err == 5 else f"GetLastError={err}"
            print(f"[错误] SendInput 注入失败（{hint}），按键没有发出去。"
                  f"请用管理员身份重开终端，或用 --diagnose 排查。")

    def key_down(self, key: str) -> None:
        self._emit(key, False)
        self._held.add(key)

    def key_up(self, key: str) -> None:
        self._emit(key, True)
        self._held.discard(key)

    def release_all(self) -> None:
        for key in list(self._held):
            self.key_up(key)


class KeybdEventBackend(Backend):
    """老式 keybd_event。某些对 SendInput 挑剔的程序反而吃这一套。"""

    name = "keybd_event"
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        if os.name != "nt":
            raise RuntimeError("keybd_event 后端仅支持 Windows")
        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._keybd_event = self._user32.keybd_event
        self._keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
        self._scancodes = {
            char: self._user32.MapVirtualKeyW(ord(char.upper()), 0) for char in ALL_KEYS
        }
        self._held: set[str] = set()

    def _emit(self, key: str, up: bool) -> None:
        flags = self.KEYEVENTF_SCANCODE | (self.KEYEVENTF_KEYUP if up else 0)
        self._keybd_event(0, self._scancodes[key], flags, None)

    def key_down(self, key: str) -> None:
        self._emit(key, False)
        self._held.add(key)

    def key_up(self, key: str) -> None:
        self._emit(key, True)
        self._held.discard(key)

    def release_all(self) -> None:
        for key in list(self._held):
            self.key_up(key)


class PostMessageBackend(Backend):
    """直接把 WM_KEYDOWN/WM_KEYUP 投递到指定窗口。

    不需要窗口获得焦点，也不受前台窗口切换影响。有些游戏（含部分 Minecraft
    客户端）只认这种方式。代价是必须知道窗口句柄，用 --window 指定。
    """

    name = "postmessage"
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_CHAR = 0x0102

    def __init__(self, hwnd: int = 0) -> None:
        import ctypes

        if os.name != "nt":
            raise RuntimeError("postmessage 后端仅支持 Windows")
        if not hwnd:
            raise RuntimeError("postmessage 后端需要 --window 指定目标窗口")
        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._hwnd = hwnd
        self._held: set[str] = set()

    @staticmethod
    def _lparam(scancode: int, up: bool) -> int:
        value = 1 | (scancode << 16)
        if up:
            value |= 0xC0000000  # bit30 前一状态 + bit31 正在释放
        return value

    def _emit(self, key: str, up: bool) -> None:
        vk = ord(key.upper())
        scancode = self._user32.MapVirtualKeyW(vk, 0)
        message = self.WM_KEYUP if up else self.WM_KEYDOWN
        self._user32.PostMessageW(self._hwnd, message, vk, self._lparam(scancode, up))

    def key_down(self, key: str) -> None:
        self._emit(key, False)
        self._held.add(key)

    def key_up(self, key: str) -> None:
        self._emit(key, True)
        self._held.discard(key)

    def release_all(self) -> None:
        for key in list(self._held):
            self.key_up(key)


class PyDirectInputBackend(Backend):
    name = "pydirectinput"

    def __init__(self) -> None:
        try:
            import pydirectinput
        except ImportError as exc:
            raise RuntimeError("未安装 pydirectinput：pip install pydirectinput") from exc

        pydirectinput.PAUSE = 0        # 关掉每步 10ms 的默认延迟
        pydirectinput.FAILSAFE = False
        self._pdi = pydirectinput
        self._held: set[str] = set()

    def key_down(self, key: str) -> None:
        self._pdi.keyDown(key)
        self._held.add(key)

    def key_up(self, key: str) -> None:
        self._pdi.keyUp(key)
        self._held.discard(key)

    def release_all(self) -> None:
        for key in list(self._held):
            self.key_up(key)


class KeyboardBackend(Backend):
    name = "keyboard"

    def __init__(self) -> None:
        try:
            import keyboard
        except ImportError as exc:
            raise RuntimeError("未安装 keyboard：pip install keyboard") from exc
        self._kb = keyboard

    def key_down(self, key: str) -> None:
        self._kb.press(key)

    def key_up(self, key: str) -> None:
        self._kb.release(key)


class PyAutoGuiBackend(Backend):
    name = "pyautogui"

    def __init__(self) -> None:
        try:
            import pyautogui
        except ImportError as exc:
            raise RuntimeError("未安装 pyautogui：pip install pyautogui") from exc
        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = False
        self._gui = pyautogui

    def key_down(self, key: str) -> None:
        self._gui.keyDown(key)

    def key_up(self, key: str) -> None:
        self._gui.keyUp(key)


BACKENDS: dict[str, Callable[..., Backend]] = {
    "sendinput": SendInputBackend,
    "pydirectinput": PyDirectInputBackend,
    "keybd_event": KeybdEventBackend,
    "postmessage": PostMessageBackend,
    "keyboard": KeyboardBackend,
    "pyautogui": PyAutoGuiBackend,
}

# auto 模式的回退顺序：优先真正注入到系统输入队列的后端
AUTO_ORDER = ["pydirectinput", "sendinput", "keybd_event", "keyboard", "pyautogui"]


def create_backend(name: str, *, hwnd: int = 0) -> Backend:
    order = AUTO_ORDER if name == "auto" else [name]
    errors: list[str] = []
    for candidate in order:
        factory = BACKENDS.get(candidate)
        if factory is None:
            errors.append(f"未知后端 {candidate}")
            continue
        try:
            backend = factory(hwnd=hwnd) if candidate == "postmessage" else factory()
        except Exception as exc:  # noqa: BLE001 - 逐个回退
            errors.append(f"{candidate}: {exc}")
            continue
        print(f"[后端] 使用 {candidate}")
        return backend
    raise SystemExit("没有可用的按键后端：\n  " + "\n  ".join(errors))


# --------------------------------------------------------------------------- #
#  计时与播放
# --------------------------------------------------------------------------- #


class _TimerResolution:
    """Windows 上把系统计时器精度提到 1ms，避免 sleep 抖动。"""

    def __init__(self) -> None:
        self._active = False
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.winmm.timeBeginPeriod(1)
                self._active = True
            except Exception:  # noqa: BLE001
                self._active = False

    def close(self) -> None:
        if self._active:
            try:
                import ctypes

                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:  # noqa: BLE001
                pass
            self._active = False


def _sleep_until(target: float, spin: float = 0.002) -> None:
    while True:
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > spin:
            time.sleep(remaining - spin)
        # 最后几毫秒自旋，保证精度


def _abort_pressed() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(ABORT_VK) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


def play(
    actions: Sequence[Action],
    backend: Backend,
    *,
    total_duration: float,
    countdown: float = 3.0,
    verbose: bool = False,
    refocus: Callable[[], bool] | None = None,
    preflight: Callable[[], str | None] | None = None,
) -> str:
    """按时间轴发送按键。返回 'done' / 'aborted' / 'blocked'。"""
    timer = _TimerResolution()
    next_report = 0.0
    last_progress = 0.0

    def _do_focus() -> None:
        if refocus is None:
            return
        if refocus():
            print("[窗口] 已切到目标窗口")
        else:
            print("[窗口] 切换到目标窗口失败，请手动点一下游戏窗口")

    try:
        if countdown > 0:
            _do_focus()
            print("[准备] 请保持游戏窗口在前台…")
            deadline = time.perf_counter() + countdown
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                sys.stdout.write(f"\r  倒计时 {remaining:4.1f}s ")
                sys.stdout.flush()
                time.sleep(min(0.1, remaining))
            sys.stdout.write("\r" + " " * 24 + "\r")
            sys.stdout.flush()

        if not actions:
            print("[结束] 时间轴为空，没有可弹的音符")
            return "done"

        # 倒计时结束再抢一次焦点，防止期间被别的窗口抢走
        if countdown > 0:
            _do_focus()

        # 此刻前台窗口应该就是游戏。如果它权限比我们高，按键一定会被丢掉，
        # 与其白弹一遍不如现在就停下来说清楚。
        if preflight is not None:
            problem = preflight()
            if problem:
                print(f"\n[错误] {problem}")
                return "blocked"

        base = time.perf_counter()
        total = total_duration

        for action in actions:
            if _abort_pressed():
                print("\n[中断] 检测到 F8，已停止")
                return "aborted"

            _sleep_until(base + action.time)
            if action.down:
                backend.key_down(action.key)
            else:
                backend.key_up(action.key)

            if verbose:
                mark = "按下" if action.down else "抬起"
                print(f"  {action.time:7.3f}s  {mark} {action.key.upper()}  ({action.name})")
            elif total > 0 and action.time >= next_report:
                next_report = action.time + 0.25
                ratio = min(action.time / total, 1.0)
                filled = int(ratio * 30)
                bar = "#" * filled + "-" * (30 - filled)
                now = time.perf_counter()
                if now - last_progress > 0.05:
                    last_progress = now
                    sys.stdout.write(f"\r  [{bar}] {ratio * 100:5.1f}%  {action.time:6.1f}/{total:.1f}s")
                    sys.stdout.flush()

        # 让最后一个音的抬起动作真正生效
        _sleep_until(base + actions[-1].time + 0.02)
        if not verbose:
            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()
        print("[完成] 弹奏结束")
        return "done"
    finally:
        backend.release_all()
        timer.close()


# --------------------------------------------------------------------------- #
#  输出与命令行
# --------------------------------------------------------------------------- #


def print_keymap() -> None:
    print()
    print("        " + "  ".join(f" {d} " for d in range(1, 8)))
    for octave in ("high", "mid", "low"):
        letters = "  ".join(f" {c.upper()} " for c in OCTAVE_KEYS[octave])
        print(f"  {OCTAVE_LABEL[octave]}音  {letters}")
    print()
    print("  音符写法：1-7 音级  ^1 高音  _1 低音  1:2 时值2拍  - 延音  0 休止  1+3+5 和弦")
    print()


def print_timeline(notes: Sequence[Note], limit: int = 150, tail: int = 30) -> None:
    print(f"\n  共 {len(notes)} 个音符")
    print("  " + "-" * 58)
    print(f"  {'时间(s)':>9}  {'键':<4} {'音名':<8} {'时值(s)':>8}")
    print("  " + "-" * 58)

    def row(note: Note) -> str:
        return f"  {note.time:9.3f}  {note.key.upper():<4} {note.name:<8} {note.duration:8.3f}"

    if len(notes) > limit + tail:
        for note in notes[:limit]:
            print(row(note))
        print(f"  ... 省略 {len(notes) - limit - tail} 个 ...")
        for note in notes[-tail:]:
            print(row(note))
    else:
        for note in notes:
            print(row(note))
    print("  " + "-" * 58)


def read_score_text(path: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "big5", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"无法识别文件编码：{path}")


def build_scale_test(hold: float = 0.35, gap: float = 0.12) -> tuple[list[Note], list[Action]]:
    """低音到高音再回来的爬音阶，专门用来确认游戏到底收不收按键。"""
    order = [("low", d) for d in range(1, 8)]
    order += [("mid", d) for d in range(1, 8)]
    order += [("high", d) for d in range(1, 8)]
    order += order[-2::-1]

    notes: list[Note] = []
    actions: list[Action] = []
    cursor = 0.0
    for octave, degree in order:
        index = OCTAVE_INDEX[octave]
        key = key_for(index, degree)
        name = f"{OCTAVE_LABEL[octave]}{degree}"
        notes.append(Note(time=cursor, key=key, duration=hold, name=name))
        actions.append(Action(cursor, key, True, name))
        actions.append(Action(cursor + hold, key, False, name))
        cursor += hold + gap
    return notes, actions


def run_diagnose(window: str | None) -> int:
    """环境自检：把「按键发不出去」的几种原因逐条排掉。"""
    import platform

    print("\n=== 环境自检 ===")
    print(f"  操作系统   : {platform.system()} {platform.release()}")
    print(f"  Python     : {platform.python_version()} ({platform.architecture()[0]})")

    if not IS_WINDOWS:
        print("  按键注入   : 非 Windows，请用 --backend keyboard / pyautogui")
        return 1

    elevated = is_self_elevated()
    print(f"  当前权限   : {'管理员' if elevated else '普通用户'}")

    # ---- 1. 权限对等性：这是「完全没反应」的头号原因 ----
    # 没有指定 --window 时，在所有可见窗口里找找哪个像是游戏（高权限的那个嫌疑最大）
    suspects: list[tuple[int, str, str]] = []
    if window:
        hwnd = find_window(window)
        if not hwnd:
            print(f"\n  [问题] 找不到标题包含 {window!r} 的窗口，用 --list-windows 看列表。")
        else:
            suspects.append((hwnd, get_window_title(hwnd), get_process_exe(get_window_pid(hwnd))))
    else:
        for hwnd, title, exe in list_windows():
            pid = get_window_pid(hwnd)
            if is_process_elevated(pid) is True:
                suspects.append((hwnd, title, exe))

    print("\n  --- 权限检查 ---")
    if not suspects:
        print("  没有发现以管理员身份运行的窗口。")
        if not elevated:
            print("  （如果你知道游戏窗口的标题，用 --diagnose --window \"标题\" 再查一次）")
    blocked_targets = []
    for hwnd, title, exe in suspects:
        target_elevated = is_process_elevated(get_window_pid(hwnd))
        mark = "[管理员]" if target_elevated else "[普通]"
        print(f"  {mark:<9} {exe:<26} {title[:36]}")
        if target_elevated and not elevated:
            blocked_targets.append(title)

    if blocked_targets:
        print("\n  >>> 找到问题了 <<<")
        print(f"  这些窗口以管理员身份运行：{'、'.join(t[:24] for t in blocked_targets)}")
        print("  而本脚本是普通权限。Windows 的 UIPI 会静默丢弃低权限进程发往")
        print("  高权限窗口的所有按键 —— 表现就是「脚本跑了，游戏毫无反应」。")
        print("  这也正是鸣潮等带 ACE 反作弊的游戏常见的情况（游戏会强制提权）。")
        print("\n  解决办法（任选其一）：")
        print("    1. 直接加 --elevate，让脚本自己弹 UAC 提权：")
        print("         python mc_piano.py scores/小星星.txt --window \"鸣潮\" --elevate")
        print("    2. 右键终端 / PowerShell → 以管理员身份运行，然后照常执行。")
        print("    3. 指定 --window 后直接跑，脚本检测到权限不足会自动请求提权。")

    # ---- 2. 按键注入本身是否可用（注意：这里只证明系统层可用） ----
    print("\n  --- 按键注入测试（左 Shift，不产生任何输入）---")
    probe = SendInputBackend()
    probe._scancodes["shift"] = 0x2A
    probe.key_down("shift")
    time.sleep(0.1)
    probe.key_up("shift")
    if probe.blocked:
        print("  [失败] SendInput 被系统拒绝，见上方错误信息。")
    else:
        print("  [通过] SendInput 调用成功，按键进入了系统输入队列。")
        print("         注意：这只说明系统层面没问题，不代表高权限窗口会接收。")

    # ---- 3. 焦点 ----
    foreground = get_foreground_window()
    fg_pid = get_window_pid(foreground) if foreground else 0
    print("\n  --- 焦点 ---")
    print(f"  当前前台   : {get_window_title(foreground) if foreground else '(无)'}")
    print(f"  前台程序   : {get_process_exe(fg_pid) if fg_pid else '(无)'}")
    if suspects:
        hwnd = suspects[0][0]
        ok = focus_window(hwnd)
        print(f"  切换焦点   : {'成功' if ok else '失败（可手动点一下游戏窗口）'}")

    if not blocked_targets and not probe.blocked:
        print("\n  权限和注入都正常。如果游戏仍然没反应，再试：")
        print("    - 用 --window \"游戏标题\" 让脚本自动切焦点")
        print("    - --backend postmessage --window \"游戏标题\"（绕开焦点限制）")
        print("    - 调大 --min-hold（比如 0.06），有些游戏要求按键按住更久")

    print("\n=== 自检结束 ===\n")
    return 0


def print_windows() -> None:
    if not IS_WINDOWS:
        print("仅 Windows 支持窗口枚举")
        return
    windows = list_windows()
    if not windows:
        print("没有找到可见窗口")
        return
    print(f"\n共有 {len(windows)} 个可见窗口：\n")
    print(f"  {'句柄':<12} {'进程':<24} 标题")
    print("  " + "-" * 76)
    for hwnd, title, exe in windows:
        print(f"  {hwnd:<12} {exe[:23]:<24} {title[:44]}")
    print("\n  用 --window \"标题里的一段字\" 指定目标窗口，例如：")
    print('    python mc_piano.py scores/小星星.txt --window "Minecraft"')
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc_piano.py",
        description="21 键 Minecraft 自动弹琴脚本（简谱文本 / MIDI 文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python mc_piano.py scores/小星星.txt\n"
            "  python mc_piano.py song.mid --track 0 --transpose -12\n"
            '  python mc_piano.py "1 2 3 1 | 3 2 1 -" --bpm 100\n'
            "  python mc_piano.py scores/小星星.txt --dry-run\n"
        ),
    )
    parser.add_argument("score", nargs="?", help="曲谱文件（.txt/.mid）或直接内联的简谱文本")
    parser.add_argument("--bpm", type=float, default=120.0, help="每分钟拍数，默认 120")
    parser.add_argument("--gate", type=float, default=0.9, help="按下时长占音符时值的比例，默认 0.9")
    parser.add_argument("--min-hold", type=float, default=0.035, help="最短按住时间（秒），默认 0.035")
    parser.add_argument("--min-gap", type=float, default=0.012, help="同键两次按下之间的最小间隔（秒），默认 0.012")
    parser.add_argument("--speed", type=float, default=1.0, help="整体速度倍率，>1 更快，默认 1.0")
    parser.add_argument("--base", type=int, default=60, help="「中音 1」的 MIDI 音高，默认 60（中央 C）")
    parser.add_argument("--transpose", type=int, default=0, help="移调半音数，默认 0")
    parser.add_argument("--snap", choices=("down", "up"), default="down", help="半音吸附方向，默认 down")
    parser.add_argument("--octave", choices=("high", "mid", "low"), default="mid", help="简谱默认八度，默认 mid")
    parser.add_argument("--track", type=int, default=None, help="MIDI：只播放指定轨道（0 起），默认合并全部")
    parser.add_argument("--quantize", type=float, default=0.0, help="MIDI：把起音对齐到该秒数网格，默认 0（不对齐）")
    parser.add_argument("--midi-engine", choices=("builtin", "mido"), default="builtin",
                        help="MIDI 解析器，默认 builtin（零依赖）")
    parser.add_argument("--countdown", type=float, default=3.0, help="开始前倒计时秒数，默认 3")
    parser.add_argument("--loop", type=int, default=1, help="循环次数，默认 1")
    parser.add_argument("--backend", choices=("auto", "pydirectinput", "sendinput", "keybd_event",
                                              "postmessage", "keyboard", "pyautogui"),
                        default="auto", help="按键后端，默认 auto（优先 pydirectinput）")
    parser.add_argument("--window", metavar="标题", default=None,
                        help="目标窗口标题的一段字。指定后会先把它切到前台再弹，"
                             "postmessage 后端必须指定")
    parser.add_argument("--elevate", action="store_true",
                        help="以管理员身份重新启动自己（游戏以管理员运行时需要）")
    parser.add_argument("--no-elevate", action="store_true",
                        help="检测到需要管理员权限时不要自动提权，只提示")
    parser.add_argument("--test", action="store_true",
                        help="弹一段从低到高的爬音阶，用来确认游戏到底收不收按键")
    parser.add_argument("--diagnose", action="store_true",
                        help="环境自检：权限、前台窗口、按键注入是否成功")
    parser.add_argument("--list-windows", action="store_true", help="列出所有可见窗口，方便拿 --window 的标题")
    parser.add_argument("--dry-run", action="store_true", help="只解析并打印时间轴，不真的按键")
    parser.add_argument("--verbose", "-v", action="store_true", help="实时打印每一次按下/抬起")
    parser.add_argument("--list-keys", action="store_true", help="打印键位表和记谱法说明后退出")
    return parser


def _load_score(args: argparse.Namespace) -> tuple[list[Note], list[Action], float, dict, list[str]]:
    """按参数载入曲谱，返回 (音符, 时间轴, 有效时长, 元信息, 警告)。"""
    source = args.score
    is_midi = source.lower().endswith((".mid", ".midi"))
    file_exists = os.path.exists(source)
    looks_like_path = (
        "/" in source
        or "\\" in source
        or source.lower().endswith((".txt", ".text", ".mid", ".midi"))
    )
    if not file_exists:
        if is_midi or looks_like_path:
            raise SystemExit(f"找不到文件：{source}")
        if len(source) > 4096:
            raise SystemExit("内联曲谱过长，请先存成文件再传入")

    if is_midi:
        notes, meta, warnings = parse_midi_score(
            source,
            track=args.track,
            base=args.base,
            transpose=args.transpose,
            snap=args.snap,
            gate=args.gate,
            quantize=args.quantize,
            engine=args.midi_engine,
        )
    else:
        text = read_score_text(source) if file_exists else source
        notes, meta, warnings = parse_text_score(
            text,
            bpm=args.bpm,
            gate=args.gate,
            default_octave=args.octave,
            base=args.base,
            transpose=args.transpose,
            snap=args.snap,
        )

    title = meta.get("title") or os.path.basename(source) or "内联曲谱"
    print(f"[曲谱] {title}")
    print(f"[信息] 音符 {len(notes)} 个 | 时长 {meta.get('duration', 0):.2f}s | "
          f"速度 {meta.get('bpm', args.bpm):g} BPM | 倍率 {args.speed:g}x")
    for warning in warnings:
        print(f"[警告] {warning}")
    if not notes:
        raise SystemExit("没有解析出任何音符，请检查曲谱内容（--list-keys 可查看记谱法）")

    timeline = build_timeline(notes, min_hold=args.min_hold, min_gap=args.min_gap, speed=args.speed)
    effective = max((n.time + max(n.duration * n.gate, 0.0) for n in notes), default=0.0) / args.speed
    return notes, timeline, effective, meta, warnings


def _resolve_window(args: argparse.Namespace) -> int:
    """把 --window 解析成句柄，返回 0 表示没问题。"""
    if not args.window:
        return 0
    if not IS_WINDOWS:
        print("[警告] --window 仅 Windows 支持，已忽略")
        return 0
    hwnd = find_window(args.window)
    if not hwnd:
        print(f"[错误] 找不到标题包含 {args.window!r} 的窗口。用 --list-windows 看看有哪些。")
        return 2
    pid = get_window_pid(hwnd)
    print(f"[窗口] 目标：{get_window_title(hwnd)}  ({get_process_exe(pid)})")
    if is_process_elevated(pid) and not is_self_elevated():
        print("[窗口] 注意：该窗口以管理员身份运行，而本脚本没有 —— 按键会被 Windows 丢弃。")
    return 0


def _run_playback(
    args: argparse.Namespace,
    notes: Sequence[Note],
    timeline: Sequence[Action],
    raw_argv: Sequence[str] = (),
) -> int:
    effective = max((n.time + max(n.duration * n.gate, 0.0) for n in notes), default=0.0) / args.speed

    hwnd = 0
    if args.window:
        status = _resolve_window(args)
        if status:
            return status
        hwnd = find_window(args.window)

    # postmessage 不能自动回退到别的后端，否则行为会跟用户预期不一致
    if args.backend == "postmessage" and not hwnd:
        print("[错误] --backend postmessage 必须配合 --window \"窗口标题\" 使用。")
        return 2

    # 目标窗口权限更高时，低权限进程发的按键会被 Windows 静默丢弃（UIPI）。
    # 这是「游戏完全没反应」最常见的原因，直接自动提权重启。
    if needs_elevation(hwnd) and not args.no_elevate:
        print(f"\n[权限] 目标窗口「{get_window_title(hwnd)}」以管理员身份运行，本脚本没有。")
        print("       Windows 会丢弃低权限进程发往高权限窗口的所有按键，所以游戏不会有任何反应。")
        print("       正在请求管理员权限重新启动…（弹 UAC 时请点“是”）")
        if relaunch_elevated(raw_argv):
            return 0
        print("       [失败] 提权被取消。请右键终端 → 以管理员身份运行。")
        return 3

    if args.elevate and not is_self_elevated():
        print("\n[权限] 按 --elevate 要求，正在以管理员身份重新启动…")
        if relaunch_elevated(raw_argv):
            return 0
        print("       [失败] 提权被取消。")
        return 3

    backend = create_backend(args.backend, hwnd=hwnd)

    def refocus() -> bool:
        if not hwnd:
            return False
        return focus_window(hwnd)

    def preflight() -> str | None:
        """正式开弹前看一眼前台窗口，权限不对就立刻停。"""
        if not IS_WINDOWS or is_self_elevated():
            return None
        foreground = get_foreground_window()
        if not foreground or not needs_elevation(foreground):
            return None
        title = get_window_title(foreground)
        return (
            f"前台窗口「{title}」以管理员身份运行。低权限进程发出的按键会被 Windows\n"
            f"        静默丢弃，弹了也不会有反应。请右键终端 → 以管理员身份运行，\n"
            f"        或者直接加 --elevate 让脚本自己提权。"
        )

    if hwnd:
        refocus()

    loops = max(1, args.loop)
    try:
        for index in range(loops):
            if loops > 1:
                print(f"\n[循环] 第 {index + 1}/{loops} 遍")
            result = play(
                timeline,
                backend,
                total_duration=effective,
                countdown=args.countdown if index == 0 else 1.0,
                verbose=args.verbose,
                refocus=refocus if hwnd else None,
                preflight=preflight,
            )
            if result == "aborted":
                return 130
            if result == "blocked":
                return 3
    except KeyboardInterrupt:
        print("\n[中断] 用户取消")
        return 130
    finally:
        backend.release_all()
        backend.close()

    if getattr(backend, "blocked", False):
        print("\n[提示] 上面出现过 SendInput 注入失败。改用管理员身份运行终端通常能解决。")
        return 4
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    args = build_parser().parse_args(argv)
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    if args.list_keys:
        print_keymap()
        return 0

    if args.list_windows:
        print_windows()
        return 0

    if args.diagnose:
        return run_diagnose(args.window)

    if args.test:
        notes, timeline = build_scale_test()
        print_keymap()
        print(f"\n[自测] 爬音阶：低1 → 高7 → 低1，共 {len(notes)} 个音，每个音 0.35 秒")
        print("       依次走 低音行 → 中音行 → 高音行。")
        print("       游戏里有声音 = 按键通路正常；完全没反应就看下面的提示。\n")
        if args.dry_run:
            print_timeline(notes)
            print("  （--dry-run：没有发送任何按键）")
            return 0
        return _run_playback(args, notes, timeline, raw_argv)

    if not args.score:
        build_parser().print_help()
        return 1

    print_keymap()
    notes, timeline, _effective, _meta, _warnings = _load_score(args)

    if args.dry_run:
        print_timeline(notes)
        print(f"  预计用时 {_effective:.2f}s，按键动作 {len(timeline)} 次（未发送任何按键）")
        # 顺手校验一下 --window，免得正式跑的时候才发现标题打错了
        if args.window:
            status = _resolve_window(args)
            if status:
                return status
            if needs_elevation(find_window(args.window)):
                print("[权限] 该窗口以管理员身份运行，正式弹奏时需要管理员权限"
                      "（加 --elevate 或指定 --window 后直接跑会自动提权）。")
        return 0

    return _run_playback(args, notes, timeline, raw_argv)


if __name__ == "__main__":
    raise SystemExit(main())
