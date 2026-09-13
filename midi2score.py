#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""midi2score.py —— 把任意 MIDI 转成这台 21 键琴弹得出来的简谱。

这台琴的硬限制决定了不能直接照搬 MIDI：

  1. 只有 3 个八度（默认低音 C3 ～ 高音 B5，MIDI 48–83），超出要折叠回来
  2. 只有白键（C 大调的 7 个音级），任何黑键都必须吸附掉
  3. 总共只有 21 个键，同一时刻按键太多要减声部

转换器会依次解决这三件事，并给出一份说明改动了什么的报告：

  python midi2score.py song.mid                  # 转换并把谱子打到标准输出
  python midi2score.py song.mid -o song.txt      # 写到文件
  python midi2score.py song.mid --report-only    # 只看分析报告
  python midi2score.py song.mid --melody         # 只保留主旋律
  python midi2score.py song.mid -o s.txt --play --window "鸣潮"

报告走 stderr、谱子走 stdout，所以 `> out.txt` 拿到的是干净的谱子。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from typing import Sequence

import mc_piano as piano

# --------------------------------------------------------------------------- #
#  乐器能力
# --------------------------------------------------------------------------- #

SCALE = (0, 2, 4, 5, 7, 9, 11)          # C 大调的 7 个音级
SCALE_SET = frozenset(SCALE)

KEY_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
IS_MINOR = (False, True, False, True, False, False, True, False, True, True, False, True)

# Krumhansl-Kessler 调性感知模板
KK_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
KK_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

# 常见量化网格（以四分音符为 1）
GRID_CHOICES = {
    "1/4": Fraction(1, 1),
    "1/8": Fraction(1, 2),
    "1/16": Fraction(1, 4),
    "1/32": Fraction(1, 8),
    "1/64": Fraction(1, 16),
}
GRID_ORDER = ["1/4", "1/8", "1/16", "1/32", "1/64"]


def lowest_pitch(base: int) -> int:
    return base - 12


def highest_pitch(base: int) -> int:
    return base + 23


# --------------------------------------------------------------------------- #
#  读 MIDI
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RawNote:
    start: int          # tick
    end: int            # tick
    pitch: int          # MIDI 音高
    velocity: int
    track: int
    channel: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(slots=True)
class SourceMidi:
    notes: list[RawNote]
    ticks_per_beat: int
    tempos: list[tuple[int, int]]      # (tick, 微秒/拍)
    track_count: int
    title: str

    def tempo_at(self, tick: int) -> int:
        tempo = 500_000
        for moment, value in self.tempos:
            if moment > tick:
                break
            tempo = value
        return tempo

    @property
    def initial_bpm(self) -> float:
        tempos = sorted(self.tempos) or [(0, 500_000)]
        return 60_000_000.0 / tempos[0][1]

    @property
    def beats(self) -> float:
        return max((n.end for n in self.notes), default=0) / self.ticks_per_beat


def load_midi(path: str) -> SourceMidi:
    tracks, tpb, title = piano._read_smf(path)
    notes: list[RawNote] = []

    for index, track in enumerate(tracks):
        active: dict[tuple[int, int], tuple[int, int]] = {}
        last_tick = 0
        for tick, status, data1, data2 in track.events:
            last_tick = max(last_tick, tick)
            high, channel = status & 0xF0, status & 0x0F
            slot = (channel, data1)
            if high == 0x90 and data2 > 0:
                active[slot] = (tick, data2)
            elif high in (0x80, 0x90):
                started = active.pop(slot, None)
                if started is not None and tick > started[0]:
                    notes.append(RawNote(started[0], tick, data1, started[1], index, channel))
        for (channel, pitch), (start, velocity) in active.items():
            if last_tick > start:
                notes.append(RawNote(start, last_tick, pitch, velocity, index, channel))

    notes.sort(key=lambda n: (n.start, n.pitch))
    tempos = sorted({item for track in tracks for item in track.tempos})
    return SourceMidi(notes, tpb, tempos or [(0, 500_000)], len(tracks), title)


# --------------------------------------------------------------------------- #
#  调性分析
# --------------------------------------------------------------------------- #


def pitch_class_histogram(notes: Sequence[RawNote]) -> list[float]:
    """按音的持续时长加权的音级直方图。"""
    histogram = [0.0] * 12
    for note in notes:
        histogram[note.pitch % 12] += max(note.length, 1)
    return histogram


def _correlate(histogram: Sequence[float], profile: Sequence[float], shift: int) -> float:
    total_h = sum(histogram) or 1.0
    total_p = sum(profile)
    h = [value / total_h for value in histogram]
    p = [value / total_p for value in profile]
    mean_h = sum(h) / 12
    mean_p = sum(p) / 12
    numerator = sum((h[(i + shift) % 12] - mean_h) * (p[i] - mean_p) for i in range(12))
    dev_h = sum((h[i] - mean_h) ** 2 for i in range(12)) ** 0.5
    dev_p = sum((p[i] - mean_p) ** 2 for i in range(12)) ** 0.5
    return numerator / (dev_h * dev_p) if dev_h and dev_p else 0.0


def detect_key(notes: Sequence[RawNote]) -> tuple[int, bool, float]:
    """返回 (主音音级 0-11, 是否小调, 相关度)。"""
    histogram = pitch_class_histogram(notes)
    best = (0, False, -2.0)
    for tonic in range(12):
        for minor, profile in ((False, KK_MAJOR), (True, KK_MINOR)):
            score = _correlate(histogram, profile, tonic)
            if score > best[2]:
                best = (tonic, minor, score)
    return best


def key_name(tonic: int, minor: bool) -> str:
    return f"{KEY_NAMES[tonic % 12]} {'小调' if minor else '大调'}"


# --------------------------------------------------------------------------- #
#  量化
# --------------------------------------------------------------------------- #


def pick_auto_grid(
    notes: Sequence[RawNote], tpb: int, bpm: float, tolerance_sec: float = 0.025
) -> tuple[str, float]:
    """挑一个最粗、但贴得住原谱的网格。

    判据是**绝对时间误差**而不是相对误差：25ms 是听觉上基本听不出来的量级。
    自由速度（rubato）演奏对任何网格的相对误差都差不多，用相对判据会永远
    掉到最细网格；用绝对判据则能得到「在不被听出来的前提下尽量规整」的结果。
    """
    starts = sorted({n.start for n in notes})
    if len(starts) < 2:
        return "1/16", 0.0

    ticks_per_second = tpb * bpm / 60.0
    tolerance_ticks = tolerance_sec * ticks_per_second

    def mean_error(name: str) -> float:
        step = float(GRID_CHOICES[name] * tpb)
        errors = [abs(start - round(start / step) * step) for start in starts]
        return sum(errors) / len(errors)

    for name in GRID_ORDER:
        error = mean_error(name)
        if error <= tolerance_ticks:
            return name, error

    finest = GRID_ORDER[-1]
    return finest, mean_error(finest)


def quantize(notes: Sequence[RawNote], step_ticks: float) -> list[RawNote]:
    result: list[RawNote] = []
    for note in notes:
        start = int(round(note.start / step_ticks) * step_ticks)
        end = int(round(note.end / step_ticks) * step_ticks)
        if end <= start:
            end = start + int(step_ticks)
        result.append(RawNote(start, end, note.pitch, note.velocity, note.track, note.channel))
    result.sort(key=lambda n: (n.start, n.pitch))
    return result


# --------------------------------------------------------------------------- #
#  选轨 / 减声部
# --------------------------------------------------------------------------- #


def drop_drums(notes: Sequence[RawNote]) -> tuple[list[RawNote], int]:
    kept = [n for n in notes if n.channel != 9]
    return kept, len(notes) - len(kept)


def pick_melody_track(notes: Sequence[RawNote]) -> int:
    """猜哪条轨道是主旋律：音多、偏高、且不是鼓。"""
    buckets: dict[int, list[RawNote]] = defaultdict(list)
    for note in notes:
        if note.channel != 9:
            buckets[note.track].append(note)
    if not buckets:
        return -1

    best_track, best_score = -1, -1.0
    for track, items in buckets.items():
        count = len(items)
        if count < 4:
            continue
        mean_pitch = sum(n.pitch for n in items) / count
        span = max(n.pitch for n in items) - min(n.pitch for n in items)
        # 音数多、音区高、跨度小 = 更像旋律
        score = count * 1.0 + mean_pitch * 2.0 - span * 0.8
        if score > best_score:
            best_track, best_score = track, score
    return best_track


def reduce_group(group: Sequence[RawNote], limit: int, mode: str) -> list[RawNote]:
    if limit <= 0 or len(group) <= limit:
        return list(group)
    ordered = sorted(group, key=lambda n: n.pitch)
    if mode == "high":
        return ordered[len(ordered) - limit:]
    if mode == "low":
        return ordered[:limit]
    if limit == 1:
        return ordered[-1:]
    # spread：保最高音（旋律）和最低音（低音），中间均匀取
    indices = {0, len(ordered) - 1}
    for slot in range(1, limit - 1):
        indices.add(round(slot * (len(ordered) - 1) / (limit - 1)))
    return [ordered[i] for i in sorted(indices)]


def group_by_onset(notes: Sequence[RawNote]) -> list[list[RawNote]]:
    groups: list[list[RawNote]] = []
    for note in sorted(notes, key=lambda n: (n.start, n.pitch)):
        if groups and groups[-1][0].start == note.start:
            groups[-1].append(note)
        else:
            groups.append([note])
    return groups


# --------------------------------------------------------------------------- #
#  移调 / 折叠 / 吸附
# --------------------------------------------------------------------------- #


def fold_into_range(pitch: int, low: int, high: int) -> tuple[int, int]:
    """把音高按八度挪进音域，返回 (新音高, 挪了几个八度)。

    音域是 36 个半音，所以任何音级都必然有一个八度落在里面 —— 这个函数是完备的，
    不会返回音域外的值。
    """
    if low <= pitch <= high:
        return pitch, 0

    # 解 low <= pitch + 12k <= high
    k_low = math.ceil((low - pitch) / 12)
    k_high = math.floor((high - pitch) / 12)
    best: int | None = None
    best_cost = 1 << 30
    for octave in range(k_low, k_high + 1):
        candidate = pitch + 12 * octave
        if low <= candidate <= high and abs(octave) < best_cost:
            best, best_cost = candidate, abs(octave)
    if best is None:  # 音域不足一个八度时的兜底，正常不会走到
        return max(low, min(high, pitch)), 0
    return best, best_cost


def choose_transposition(
    notes: Sequence[RawNote], low: int, high: int
) -> tuple[int, int, float]:
    """搜索最佳的「移调半音数 + 八度偏移」。

    评分只看转换之后真正会发生什么：落在白键上的时长给分，
    需要吸附的黑键扣分，需要单独折叠八度的音再扣分。
    """
    weights = [max(n.length, 1) for n in notes]
    total = float(sum(weights)) or 1.0
    best_score = float("-inf")
    best_shift = best_octave = 0

    for shift in range(12):
        for octave in range(-3, 4):
            score = 0.0
            for note, weight in zip(notes, weights):
                moved, folds = fold_into_range(note.pitch + shift + 12 * octave, low, high)
                if moved % 12 in SCALE_SET:
                    score += weight
                else:
                    score -= weight * 0.6
                score -= folds * weight * 0.5
            # 同等条件下偏好移调量小的（少改调就更接近原曲）
            score -= abs(shift if shift <= 6 else shift - 12) * total * 0.002
            # 分数完全相同时，偏好八度偏移小的 —— 音域内能放下就别乱挪音区
            if score > best_score or (
                score == best_score and abs(octave) < abs(best_octave)
            ):
                best_score, best_shift, best_octave = score, shift, octave

    # 把 0..11 归一化成 -6..+5，同时把多减掉的那个八度补回来
    if best_shift <= 6:
        signed, octave = best_shift, best_octave
    else:
        signed, octave = best_shift - 12, best_octave + 1
    return signed, octave, best_score / total


def snap_to_scale(pitch: int, previous: int | None, following: int | None, prefer: str) -> int:
    """黑键吸附到相邻白键。用前后音做参照，尽量让旋律线平滑。"""
    if pitch % 12 in SCALE_SET:
        return pitch

    def cost(candidate: int) -> float:
        value = 0.0
        if previous is not None:
            value += abs(candidate - previous)
        if following is not None:
            value += abs(candidate - following)
        if prefer == "up" and candidate > pitch:
            value -= 0.4
        if prefer == "down" and candidate < pitch:
            value -= 0.4
        return value

    return min((pitch - 1, pitch + 1), key=cost)


@dataclass(slots=True)
class ScoreNote:
    start: int          # tick（量化后）
    end: int            # tick
    pitch: int          # 最终音高
    name: str           # 中5 / 高1 这样的显示名
    key: str            # 键位字母
    prefix: str         # 简谱八度前缀 ^ / _ / 空
    degree: int         # 简谱音级 1-7
    original: int       # 原始音高

    @property
    def token(self) -> str:
        return f"{self.prefix}{self.degree}"


@dataclass(slots=True)
class ConversionReport:
    total: int = 0
    dropped_drums: int = 0
    dropped_polyphony: int = 0
    snapped: int = 0
    folded: int = 0
    collapsed: int = 0
    sustain_trimmed: int = 0
    transposed: int = 0
    shift: int = 0
    octave: int = 0
    fit: float = 0.0
    grid: str = ""
    quantize_error: float = 0.0
    rubato: bool = False
    source_key: str = ""
    result_key: str = ""
    source_low: int = 0
    source_high: int = 0
    result_low: int = 0
    result_high: int = 0
    source_octaves: float = 0.0
    result_octaves: float = 0.0
    track_used: str = ""
    dominant_beats: float = 0.0
    dominant_share: float = 0.0


def convert(
    source: SourceMidi,
    *,
    base: int = 60,
    track: int | None = None,
    melody_only: bool = False,
    max_notes: int = 3,
    reduce_mode: str = "spread",
    grid: str = "auto",
    transpose: int | None = None,
    octave: int | None = None,
    snap: str = "smart",
    min_velocity: int = 1,
    drop_drum_channel: bool = True,
    bar_beats: int = 4,
) -> tuple[list[ScoreNote], ConversionReport, list[tuple[float, float]]]:
    """返回 (谱面音符, 报告, 速度变化表[(拍, bpm)])。"""
    report = ConversionReport()
    low, high = lowest_pitch(base), highest_pitch(base)
    notes = list(source.notes)

    # ---- 1. 过滤明显不要的音 ----
    if drop_drum_channel:
        notes, report.dropped_drums = drop_drums(notes)
    notes = [n for n in notes if n.velocity >= min_velocity]
    if not notes:
        raise SystemExit("这个 MIDI 里没有找到任何可用的音符")

    if track is not None:
        notes = [n for n in notes if n.track == track]
        report.track_used = f"第 {track} 轨"
        if not notes:
            raise SystemExit(f"第 {track} 轨上没有任何音符")
    elif melody_only:
        chosen = pick_melody_track(notes)
        if chosen >= 0:
            notes = [n for n in notes if n.track == chosen]
            report.track_used = f"自动选中第 {chosen} 轨（主旋律）"
    if not report.track_used:
        used = sorted({n.track for n in notes})
        if len(used) == 1:
            report.track_used = (f"第 {used[0]} 轨"
                                 f"（{source.track_count} 条轨道里只有这条有音符）")
        else:
            report.track_used = f"第 {'、'.join(map(str, used))} 轨合并"

    report.source_low = min(n.pitch for n in notes)
    report.source_high = max(n.pitch for n in notes)
    source_tonic, source_minor, _ = detect_key(notes)
    report.source_key = key_name(source_tonic, source_minor)

    # ---- 2. 量化 ----
    if grid == "auto":
        grid_name, error = pick_auto_grid(notes, source.ticks_per_beat, source.initial_bpm)
        step = float(GRID_CHOICES[grid_name] * source.ticks_per_beat)
        # 连最细的网格都贴不住，说明是自由速度演奏，不是节拍不准
        report.rubato = error > step * 0.15
    else:
        grid_name, error = grid, 0.0
    report.grid, report.quantize_error = grid_name, error

    if grid_name in GRID_CHOICES:
        step = float(GRID_CHOICES[grid_name] * source.ticks_per_beat)
        notes = quantize(notes, step)
        # 量化后同一时刻同一音高的重复音去掉
        deduped: dict[tuple[int, int], RawNote] = {}
        for note in notes:
            slot = (note.start, note.pitch)
            if slot not in deduped or note.length > deduped[slot].length:
                deduped[slot] = note
        notes = sorted(deduped.values(), key=lambda n: (n.start, n.pitch))

    # ---- 3. 减声部 ----
    groups = group_by_onset(notes)
    limit = 1 if melody_only else max_notes
    reduced: list[RawNote] = []
    for group in groups:
        kept = reduce_group(group, limit, reduce_mode)
        report.dropped_polyphony += len(group) - len(kept)
        reduced.extend(kept)
    notes = reduced
    report.total = len(notes)
    # ---- 4. 移调 ----
    if transpose is None:
        shift, auto_octave, fit = choose_transposition(notes, low, high)
        report.shift, report.fit = shift, fit
        if octave is None:
            octave = auto_octave
    else:
        shift, fit = transpose, 0.0
        report.shift, report.fit = shift, fit
        if octave is None:
            octave = 0
    report.octave = octave or 0

    moved: list[tuple[RawNote, int]] = []
    for note in notes:
        pitch = note.pitch + report.shift + 12 * report.octave
        folded, folds = fold_into_range(pitch, low, high)
        if folds:
            report.folded += 1
        if pitch != note.pitch:
            report.transposed += 1
        moved.append((note, folded))

    # ---- 5. 吸附黑键（前后音做参照，让旋律线尽量平滑）----
    preference = "down" if snap in ("down", "smart") else "up"
    final: list[ScoreNote] = []
    for index, (note, pitch) in enumerate(moved):
        previous = moved[index - 1][1] if index > 0 else None
        following = moved[index + 1][1] if index + 1 < len(moved) else None
        snapped = snap_to_scale(pitch, previous, following, preference)
        if snapped != pitch:
            report.snapped += 1
        snapped, _ = fold_into_range(snapped, low, high)
        position = snapped - base
        octave_index = max(-1, min(1, position // 12))
        degree = SCALE.index(snapped % 12) + 1
        final.append(
            ScoreNote(
                start=note.start,
                end=note.end,
                pitch=snapped,
                name=f"{piano.OCTAVE_LABEL[piano.INDEX_OCTAVE[octave_index]]}{degree}",
                key=piano.key_for(octave_index, degree),
                prefix="^" if octave_index > 0 else ("_" if octave_index < 0 else ""),
                degree=degree,
                original=note.pitch,
            )
        )

    # ---- 6. 同一个键上时值重叠的截断，避免谱面自相矛盾 ----
    by_key: dict[str, list[ScoreNote]] = defaultdict(list)
    for note in final:
        by_key[note.key].append(note)
    collapsed = 0
    for items in by_key.values():
        items.sort(key=lambda n: n.start)
        for index, note in enumerate(items[:-1]):
            next_start = items[index + 1].start
            if note.end > next_start:
                note.end = next_start
                collapsed += 1
    report.collapsed = collapsed
    final = [n for n in final if n.end > n.start]
    final.sort(key=lambda n: (n.start, n.pitch))
    report.total = len(final)  # 报告里的「输出音符」以最终写进谱子的数量为准

    # 简谱是顺序语义，一个音只能在下一个音开始前结束。
    # 统计有多少个和弦因为「后面还有别的音要先弹」而被缩短了。
    onset_groups: list[list[ScoreNote]] = []
    for note in final:
        if onset_groups and onset_groups[-1][0].start == note.start:
            onset_groups[-1].append(note)
        else:
            onset_groups.append([note])
    trimmed = 0
    for index, group in enumerate(onset_groups[:-1]):
        if max(n.end for n in group) > onset_groups[index + 1][0].start:
            trimmed += 1
    report.sustain_trimmed = trimmed

    report.result_low = min((n.pitch for n in final), default=0)
    report.result_high = max((n.pitch for n in final), default=0)
    result_notes = [
        RawNote(n.start, n.end, n.pitch, 100, 0, 0) for n in final
    ]
    result_tonic, result_minor, _ = detect_key(result_notes)
    report.result_key = key_name(result_tonic, result_minor)
    report.source_octaves = (report.source_high - report.source_low) / 12
    report.result_octaves = (report.result_high - report.result_low) / 12

    # 主导时值：用来判断文件里的速度标记是不是默认值乱填的
    if notes:
        lengths = [round(n.length / source.ticks_per_beat, 2) for n in notes]
        counts: dict[float, int] = defaultdict(int)
        for value in lengths:
            counts[round(value * 10) / 10] += 1
        best = max(counts.items(), key=lambda item: item[1])
        report.dominant_beats = best[0]
        report.dominant_share = best[1] / len(lengths)

    # ---- 7. 速度变化表（拍, bpm）----
    tempo_points: list[tuple[float, float]] = []
    for tick, tempo in sorted(source.tempos):
        beat = tick / source.ticks_per_beat
        bpm = round(60_000_000.0 / tempo, 3)
        if tempo_points and abs(tempo_points[-1][1] - bpm) < 1e-6:
            continue
        tempo_points.append((beat, bpm))
    if not tempo_points:
        tempo_points = [(0.0, round(source.initial_bpm, 3))]

    _ = bar_beats
    return final, report, tempo_points


# --------------------------------------------------------------------------- #
#  写出简谱
# --------------------------------------------------------------------------- #


def format_beats(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def render_score(
    notes: Sequence[ScoreNote],
    source: SourceMidi,
    *,
    title: str,
    tempo_points: Sequence[tuple[float, float]],
    bar_beats: int = 4,
    bars_per_line: int = 4,
    hold: str = "gap",
) -> str:
    """把谱面音符渲染成简谱文本。

    简谱是顺序语义 —— 每个音从前一个音的结束处开始。所以默认（hold="gap"）
    把每个和弦的时值取成「到下一个起音的间隔」，这样时间轴和 MIDI 严格对齐。
    代价是被按住的长音会变短；`hold="true"` 保留真实时值，但只要有音符重叠，
    后面所有音就会整体后移。
    """
    tpb = source.ticks_per_beat
    lines: list[str] = []

    lines.append("// 由 midi2score.py 从 MIDI 转换而来")
    pitches = [n.pitch for n in notes]
    if pitches:
        lines.append(f"// 转换后音域 MIDI {min(pitches)}–{max(pitches)}，"
                     f"全部落在这台琴的 3 个白键八度内")
    lines.append(f"@title={title}")

    # 速度变化作为行内指令插入，位置精确到音符之前
    pending = list(tempo_points)
    if pending and pending[0][0] <= 0:
        lines.append(f"@bpm={pending.pop(0)[1]:g}")
    else:
        lines.append(f"@bpm={source.initial_bpm:g}")

    groups: list[list[ScoreNote]] = []
    for note in notes:
        if groups and groups[-1][0].start == note.start:
            groups[-1].append(note)
        else:
            groups.append([note])

    tokens: list[tuple[Fraction, str]] = []
    cursor = Fraction(0)
    for index, group in enumerate(groups):
        start = Fraction(group[0].start, tpb)
        if hold == "gap" and index + 1 < len(groups):
            end = Fraction(groups[index + 1][0].start, tpb)
        else:
            end = Fraction(max(n.end for n in group), tpb)
        if end <= start:
            end = start + Fraction(1, 4)

        if start > cursor:
            tokens.append((cursor, f"0:{format_beats(start - cursor)}"))

        # 吸附之后和弦里可能出现重复音级，去重
        parts: list[str] = []
        for note in sorted(group, key=lambda x: x.pitch):
            if note.token not in parts:
                parts.append(note.token)
        tokens.append((start, f"{'+'.join(parts)}:{format_beats(end - start)}"))
        cursor = max(cursor, end)

    # 排版：每 bar_beats 拍画一个小节线，每 bars_per_line 个小节换一行
    buffer: list[str] = []
    bars_in_line = 0
    last_bar = 0
    for beat, text in tokens:
        bar = int(beat / bar_beats)
        while bar > last_bar:
            buffer.append("|")
            last_bar += 1
            bars_in_line += 1
            if bars_in_line >= bars_per_line:
                lines.append(" ".join(buffer))
                buffer = []
                bars_in_line = 0
        # 速度变化插在对应音符前面
        while pending and pending[0][0] <= float(beat):
            change_beat, change_bpm = pending.pop(0)
            if buffer:
                lines.append(" ".join(buffer))
                buffer = []
                bars_in_line = 0
            lines.append(f"@bpm={change_bpm:g}")
        buffer.append(text)

    if buffer:
        lines.append(" ".join(buffer))

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  报告
# --------------------------------------------------------------------------- #


def print_report(report: ConversionReport, source: SourceMidi, out) -> None:
    def line(text: str = "") -> None:
        print(text, file=out)

    line()
    line("=== 转换报告 ===")
    if source.title and source.title != report.track_used:
        line(f"  MIDI 内部名 : {source.title}")
    line(f"  使用轨     : {report.track_used}")
    line(f"  量化网格   : {'不量化' if report.grid == 'off' else report.grid}"
         + (f"（平均误差 {report.quantize_error:.1f} tick）" if report.quantize_error else ""))
    if report.rubato:
        line("               检测到自由速度（rubato）演奏 —— 原曲本来就没对齐任何节拍网格，")
        line("               已退到最细网格只做轻微对齐，不会硬掰节奏。可用 --grid off 完全不动。")
    line(f"  原曲调性   : {report.source_key}")
    line(f"  原曲音域   : MIDI {report.source_low}–{report.source_high}"
         f"（{report.source_octaves:.1f} 个八度）")
    line()
    line(f"  移调       : {report.shift:+d} 半音"
         + (f"，再整体移动 {report.octave:+d} 个八度" if report.octave else ""))
    line(f"  转换后调性 : {report.result_key}")
    line(f"  转换后音域 : MIDI {report.result_low}–{report.result_high}"
         f"（{report.result_octaves:.1f} 个八度）")
    line()
    line(f"  输出音符   : {report.total} 个")
    if report.dropped_drums:
        line(f"  去掉鼓轨   : {report.dropped_drums} 个（第 10 通道，这台琴表达不了）")
    if report.dropped_polyphony:
        line(f"  减声部     : {report.dropped_polyphony} 个（同一时刻按键太多）")
    if report.folded:
        line(f"  八度折叠   : {report.folded} 个（超出 3 个八度，挪回音域内）")
    if report.snapped:
        line(f"  半音吸附   : {report.snapped} 个（黑键吸附到相邻白键）")
    if report.collapsed:
        line(f"  时值截断   : {report.collapsed} 个（同键上时值撞车）")
    if report.sustain_trimmed:
        line(f"  长音缩短   : {report.sustain_trimmed} 处（简谱是顺序的，撑不到下一个音的"
             f"长音必须提前收；用 --hold true 可保留真实时值，但时间轴会整体后移）")
    line()
    if report.dominant_share > 0.1:
        line(f"  主导时值   : {report.dominant_beats:g} 拍"
             f"（占 {report.dominant_share * 100:.0f}% 的音符）")
        if abs(report.dominant_beats - round(report.dominant_beats)) > 0.15:
            line("               常见时值不是整数拍 —— 这个文件的速度标记很可能是默认填的。")
            line("               弹出来快慢不对的话，用 --bpm 指定真实速度按耳朵校正。")
    line()


# --------------------------------------------------------------------------- #
#  命令行
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="midi2score.py",
        description="把 MIDI 转成 21 键琴能弹的简谱",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python midi2score.py song.mid\n"
            "  python midi2score.py song.mid -o song.txt --melody\n"
            "  python midi2score.py song.mid --report-only\n"
            "  python midi2score.py song.mid -o s.txt --play --window \"鸣潮\"\n"
        ),
    )
    parser.add_argument("midi", help="输入 MIDI 文件")
    parser.add_argument("-o", "--output", help="输出的简谱文件，默认打到标准输出")
    parser.add_argument("--base", type=int, default=60, help="「中音 1」的 MIDI 音高，默认 60")
    parser.add_argument("--track", type=int, default=None, help="只用指定的 MIDI 轨道（0 起）")
    parser.add_argument("--melody", action="store_true", help="只保留主旋律（自动选轨 + 单音）")
    parser.add_argument("--max-notes", type=int, default=3,
                        help="同一时刻最多几个音，0 = 不限，默认 3")
    parser.add_argument("--reduce", choices=("spread", "high", "low"), default="spread",
                        help="减声部时保留哪些音，默认 spread（保旋律和低音）")
    parser.add_argument("--grid", choices=(*GRID_CHOICES, "auto", "off"), default="auto",
                        help="节奏量化网格，默认 auto（自动挑最贴合的）")
    parser.add_argument("--transpose", default="auto",
                        help="移调半音数，或 auto（默认，搜索最合适的调）")
    parser.add_argument("--octave", type=int, default=None, help="额外的八度偏移，默认自动")
    parser.add_argument("--snap", choices=("smart", "down", "up"), default="smart",
                        help="黑键吸附方向，默认 smart（跟随旋律走向）")
    parser.add_argument("--min-velocity", type=int, default=1, help="低于该力度的音丢掉，默认 1")
    parser.add_argument("--keep-drums", action="store_true", help="保留第 10 通道的鼓")
    parser.add_argument("--bar", type=int, default=4, help="每小节几拍（只影响排版），默认 4")
    parser.add_argument("--bars-per-line", type=int, default=4, help="每行几个小节，默认 4")
    parser.add_argument("--hold", choices=("gap", "true"), default="gap",
                        help="和弦时值怎么取：gap=到下一个音的间隔（默认，时间轴严格对齐）；"
                             "true=用 MIDI 真实时值（只适合几乎没有重叠延音的谱）")
    parser.add_argument("--bpm", type=float, default=None,
                        help="改写输出的速度。文件里的速度标记得不对时，用它按耳朵校正"
                             "（相对速度变化会保留）")
    parser.add_argument("--report-only", action="store_true", help="只打印分析报告，不输出谱子")
    parser.add_argument("--no-report", action="store_true", help="不打印报告")
    parser.add_argument("--play", action="store_true", help="转换完直接调用 mc_piano.py 弹奏")
    parser.add_argument("--window", metavar="标题", help="配合 --play：目标游戏窗口")
    parser.add_argument("--elevate", action="store_true", help="配合 --play：请求管理员权限")
    parser.add_argument("--countdown", type=float, default=3.0, help="配合 --play：倒计时秒数")
    parser.add_argument("--speed", type=float, default=1.0, help="配合 --play：速度倍率")
    parser.add_argument("--backend", default="auto", help="配合 --play：按键后端")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    args = build_parser().parse_args(argv)

    if not os.path.exists(args.midi):
        raise SystemExit(f"找不到文件：{args.midi}")

    transpose: int | None
    if args.transpose == "auto":
        transpose = None
    else:
        transpose = int(args.transpose)

    source = load_midi(args.midi)
    if not source.notes:
        raise SystemExit("这个 MIDI 里没有音符")

    notes, report, tempo_points = convert(
        source,
        base=args.base,
        track=args.track,
        melody_only=args.melody,
        max_notes=args.max_notes,
        reduce_mode=args.reduce,
        grid=args.grid,
        transpose=transpose,
        octave=args.octave,
        snap=args.snap,
        min_velocity=args.min_velocity,
        drop_drum_channel=not args.keep_drums,
        bar_beats=args.bar,
    )

    # --bpm 只改播放速度，保留相对的速度变化
    if args.bpm and tempo_points:
        scale = args.bpm / tempo_points[0][1]
        tempo_points = [(beat, round(bpm * scale, 3)) for beat, bpm in tempo_points]

    if not args.no_report:
        # 报告走 stderr，这样 `> out.txt` 拿到的谱子是干净的
        print_report(report, source, sys.stderr if not args.output else sys.stdout)

    if args.report_only:
        return 0

    # 标题用文件名 —— MIDI 内部第一轨的名字经常是 "Conductor" 之类的无意义内容
    title = os.path.splitext(os.path.basename(args.midi))[0]
    text = render_score(
        notes, source, title=title, tempo_points=tempo_points,
        bar_beats=args.bar, bars_per_line=args.bars_per_line, hold=args.hold,
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"[输出] 已写入 {args.output}（{len(notes)} 个音符）", file=sys.stdout)
    else:
        sys.stdout.write(text)

    if args.play:
        target = args.output
        if not target:
            target = os.path.splitext(args.midi)[0] + ".score.txt"
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"[输出] 已写入 {target}", file=sys.stdout)
        play_argv = [target, "--countdown", str(args.countdown), "--speed", str(args.speed),
                     "--backend", args.backend]
        if args.window:
            play_argv += ["--window", args.window]
        if args.elevate:
            play_argv += ["--elevate"]
        print("[弹奏] 交给 mc_piano.py…", file=sys.stdout)
        return piano.main(play_argv)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
