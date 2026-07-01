#!/usr/bin/env python3
"""Build an academic-style HTML report for EPIC-KITCHENS-100.

The report is generated from local metadata plus the already audited remote
video-size summary. Charts are exported as PNG assets and embedded in the HTML
for portable viewing.
"""

from __future__ import annotations

import base64
import csv
import html
import math
import os
import textwrap
from collections import Counter
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path("reports/epic_kitchens_100/.mplcache").resolve()))

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.patches import Patch


ROOT = Path("data/external/epic-kitchens-100-annotations")
DOWNLOAD = Path("data/external/epic-kitchens-download-scripts-100")
PROCESSED = Path("data/processed/epic_kitchens_100")
REPORT_DIR = Path("reports/epic_kitchens_100")
ASSET_DIR = REPORT_DIR / "assets"
REPORT_PATH = REPORT_DIR / "dataset_report.html"
SOURCE_NOTES_PATH = REPORT_DIR / "dataset_report_source_notes.md"


RAW_SIZE_BY_SOURCE_GB = {
    "EPIC-KITCHENS-55 原有视频": 748.926,
    "EPIC-KITCHENS-100 新增扩展": 498.140,
}
RAW_TOTAL_GB = sum(RAW_SIZE_BY_SOURCE_GB.values())
PROXY_TOTAL_GB_EST = 40.24
PROXY_TOTAL_GB_BUDGET_LOW = 45
PROXY_TOTAL_GB_BUDGET_HIGH = 60


FONT_FAMILY = [
    "PingFang SC",
    "Hiragino Sans GB",
    "Songti SC",
    "Arial Unicode MS",
    "Noto Sans CJK SC",
    "Aptos",
    "Inter",
    "Segoe UI",
    "DejaVu Sans",
    "Arial",
    "sans-serif",
]
MONO_FONT_FAMILY = ["SF Mono", "Menlo", "Consolas", "DejaVu Sans Mono", "monospace"]

TOKENS = {
    "surface": "#FCFCFD",
    "panel": "#FFFFFF",
    "ink": "#1F2430",
    "muted": "#6F768A",
    "grid": "#E6E8F0",
    "axis": "#D7DBE7",
}

NEUTRAL_MARKS = {
    "open": TOKENS["panel"],
    "xlight": "#F4F5F7",
    "light": "#E2E5EA",
    "base": "#C5CAD3",
    "mid": "#7A828F",
    "dark": "#464C55",
}

COLOR_FAMILIES = {
    "blue": {
        "open": TOKENS["panel"],
        "xlight": "#EAF1FE",
        "light": "#CEDFFE",
        "base": "#A3BEFA",
        "mid": "#5477C4",
        "dark": "#2E4780",
    },
    "gold": {
        "open": TOKENS["panel"],
        "xlight": "#FFF4C2",
        "light": "#FFEA8F",
        "base": "#FFE15B",
        "mid": "#B8A037",
        "dark": "#736422",
    },
    "orange": {
        "open": TOKENS["panel"],
        "xlight": "#FFEDDE",
        "light": "#FFBDA1",
        "base": "#F0986E",
        "mid": "#CC6F47",
        "dark": "#804126",
    },
    "olive": {
        "open": TOKENS["panel"],
        "xlight": "#D8ECBD",
        "light": "#BEEB96",
        "base": "#A3D576",
        "mid": "#71B436",
        "dark": "#386411",
    },
    "pink": {
        "open": TOKENS["panel"],
        "xlight": "#FCDAD6",
        "light": "#F5BACC",
        "base": "#F390CA",
        "mid": "#BD569B",
        "dark": "#8A3A6F",
    },
}

VERB_LABEL_ZH = {
    "take": "拿取",
    "put": "放置",
    "wash": "清洗",
    "open": "打开",
    "close": "关闭",
    "insert": "放入",
    "turn-on": "开启",
    "turn-off": "关停",
    "cut": "切分",
    "mix": "搅拌",
    "pour": "倾倒",
    "move": "移动",
    "remove": "移除",
    "throw": "丢弃",
    "dry": "擦干",
    "shake": "摇晃",
    "adjust": "调整",
    "peel": "削皮",
    "squeeze": "挤压",
    "scoop": "舀取",
}

NOUN_LABEL_ZH = {
    "tap": "水龙头",
    "plate": "盘子",
    "spoon": "勺子",
    "cupboard": "橱柜",
    "knife": "刀",
    "pan": "锅",
    "lid": "盖子",
    "bowl": "碗",
    "drawer": "抽屉",
    "glass": "玻璃杯",
    "sponge": "海绵",
    "hand": "手",
    "fridge": "冰箱",
    "cup": "杯子",
    "fork": "叉子",
    "cloth": "抹布",
    "bag": "袋子",
    "bottle": "瓶子",
    "board:chopping": "砧板",
    "container": "容器",
}

NOUN_CATEGORY_ZH = {
    "appliances": "家电和固定设备",
    "crockery": "餐具",
    "containers": "容器",
    "cutlery": "刀叉勺",
    "vegetables": "蔬菜",
    "utensils": "厨具",
    "cleaning equipment and material": "清洁用品",
    "cookware": "炊具",
    "baked goods and grains": "烘焙食品和谷物",
    "furniture": "家具",
    "spices and herbs and sauces": "调料和酱料",
    "storage": "储物设施",
    "materials": "材料",
    "meat and substitute": "肉类及替代品",
    "dairy and eggs": "乳制品和蛋",
    "prepared food": "预制食品",
    "hand": "手",
    "rubbish": "垃圾",
    "drinks": "饮料",
    "fruits and nuts": "水果和坚果",
}

SUGGESTED_USE_ZH = {
    "direct_session": "可直接作为会话",
    "cut_into_sessions": "建议切成多个会话",
    "short_support_session": "可作短辅助会话",
    "low_priority": "低优先级",
}


def use_chart_theme() -> None:
    sns.set_theme(
        style="whitegrid",
        rc={
            "figure.facecolor": TOKENS["surface"],
            "figure.edgecolor": "none",
            "savefig.facecolor": TOKENS["surface"],
            "savefig.edgecolor": "none",
            "axes.facecolor": TOKENS["panel"],
            "axes.edgecolor": TOKENS["axis"],
            "axes.labelcolor": TOKENS["ink"],
            "axes.grid": True,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": TOKENS["grid"],
            "grid.linewidth": 0.8,
            "font.family": "sans-serif",
            "font.sans-serif": FONT_FAMILY,
            "font.monospace": MONO_FONT_FAMILY,
            "axes.unicode_minus": False,
            "patch.linewidth": 1.0,
        },
    )


def add_chart_header(fig, ax, title, subtitle, *, title_width=58, subtitle_width=96) -> None:
    title = textwrap.fill(str(title).strip(), width=title_width, break_long_words=False)
    subtitle = textwrap.fill(str(subtitle).strip(), width=subtitle_width, break_long_words=False)
    title_lines = title.count("\n") + 1
    subtitle_lines = subtitle.count("\n") + 1
    ax.set_title("")
    fig.subplots_adjust(top=max(0.64, 0.86 - 0.05 * (title_lines - 1) - 0.032 * (subtitle_lines - 1)))
    left = ax.get_position().x0
    fig.text(
        left,
        0.985,
        title,
        ha="left",
        va="top",
        fontsize=13,
        fontweight="semibold",
        color=TOKENS["ink"],
        linespacing=1.08,
    )
    fig.text(
        left,
        0.925 - 0.045 * (title_lines - 1),
        subtitle,
        ha="left",
        va="top",
        fontsize=9.2,
        color=TOKENS["muted"],
        linespacing=1.18,
    )
    sns.despine(ax=ax)


def save_fig(fig, name: str) -> Path:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    path = ASSET_DIR / f"{name}.png"
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor=TOKENS["surface"])
    plt.close(fig)
    return path


def image_to_data_uri(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def parse_timestamp(value: str) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return math.nan
    value = str(value).strip()
    if not value or value.lower() == "nan":
        return math.nan
    parts = value.split(":")
    if len(parts) != 3:
        return math.nan
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def source_subset(video_id: str) -> str:
    suffix = int(video_id.split("_", 1)[1])
    if suffix >= 100:
        return "EPIC-KITCHENS-100 新增扩展"
    return "EPIC-KITCHENS-55 原有视频"


def fmt_num(value: float, digits: int = 1) -> str:
    return f"{value:,.{digits}f}"


def fmt_int(value: int | float) -> str:
    return f"{int(round(value)):,}"


def load_data() -> dict[str, pd.DataFrame]:
    video_info = pd.read_csv(ROOT / "EPIC_100_video_info.csv")
    train = pd.read_csv(ROOT / "EPIC_100_train.csv")
    val = pd.read_csv(ROOT / "EPIC_100_validation.csv")
    test = pd.read_csv(ROOT / "EPIC_100_test_timestamps.csv")
    verb_classes = pd.read_csv(ROOT / "EPIC_100_verb_classes.csv")
    noun_classes = pd.read_csv(ROOT / "EPIC_100_noun_classes.csv")
    splits = pd.read_csv(DOWNLOAD / "data/epic_100_splits.csv")
    candidate_participants = pd.read_csv(PROCESSED / "candidate_participants.csv")
    candidate_videos = pd.read_csv(PROCESSED / "candidate_videos.csv")

    video_info["participant_id"] = video_info["video_id"].str.split("_").str[0]
    video_info["duration_min"] = video_info["duration"] / 60
    video_info["duration_h"] = video_info["duration"] / 3600
    video_info["source_subset"] = video_info["video_id"].map(source_subset)

    labelled = pd.concat([train.assign(split="train"), val.assign(split="validation")], ignore_index=True)
    labelled["start_sec"] = labelled["start_timestamp"].map(parse_timestamp)
    labelled["stop_sec"] = labelled["stop_timestamp"].map(parse_timestamp)
    labelled["action_duration_sec"] = labelled["stop_sec"] - labelled["start_sec"]
    labelled = labelled[labelled["action_duration_sec"].ge(0) | labelled["action_duration_sec"].isna()].copy()

    video_action_counts = labelled.groupby("video_id").size().rename("labelled_actions").reset_index()
    video_info = video_info.merge(video_action_counts, on="video_id", how="left")
    video_info["labelled_actions"] = video_info["labelled_actions"].fillna(0)
    video_info["actions_per_min"] = video_info["labelled_actions"] / video_info["duration_min"].replace(0, np.nan)

    return {
        "video_info": video_info,
        "train": train,
        "val": val,
        "test": test,
        "labelled": labelled,
        "verb_classes": verb_classes,
        "noun_classes": noun_classes,
        "splits": splits,
        "candidate_participants": candidate_participants,
        "candidate_videos": candidate_videos,
    }


def make_charts(data: dict[str, pd.DataFrame]) -> dict[str, Path]:
    use_chart_theme()
    charts: dict[str, Path] = {}
    video_info = data["video_info"]
    labelled = data["labelled"]
    train = data["train"]
    val = data["val"]
    test = data["test"]
    verb_classes = data["verb_classes"]
    noun_classes = data["noun_classes"]

    # 1. Source composition.
    source_duration = video_info.groupby("source_subset", as_index=False).agg(
        video_count=("video_id", "count"),
        duration_h=("duration_h", "sum"),
    )
    source_duration["raw_size_gb"] = source_duration["source_subset"].map(RAW_SIZE_BY_SOURCE_GB)
    metric_rows = []
    for metric, label in [
        ("video_count", "视频数量"),
        ("duration_h", "视频时长"),
        ("raw_size_gb", "原始体积"),
    ]:
        total = source_duration[metric].sum()
        for _, row in source_duration.iterrows():
            metric_rows.append(
                {
                    "metric": label,
                    "source_subset": row["source_subset"],
                    "share": row[metric] / total * 100,
                }
            )
    comp = pd.DataFrame(metric_rows)
    fig, ax = plt.subplots(figsize=(9.4, 4.9))
    group_order = ["视频数量", "视频时长", "原始体积"]
    segment_order = ["EPIC-KITCHENS-55 原有视频", "EPIC-KITCHENS-100 新增扩展"]
    y = np.arange(len(group_order))
    left = np.zeros(len(group_order))
    colors = {
        segment_order[0]: COLOR_FAMILIES["blue"]["base"],
        segment_order[1]: COLOR_FAMILIES["orange"]["base"],
    }
    edges = {
        segment_order[0]: COLOR_FAMILIES["blue"]["dark"],
        segment_order[1]: COLOR_FAMILIES["orange"]["dark"],
    }
    for segment in segment_order:
        values = (
            comp[comp["source_subset"].eq(segment)]
            .set_index("metric")
            .reindex(group_order)["share"]
            .fillna(0)
            .to_numpy()
        )
        ax.barh(y, values, left=left, color=colors[segment], edgecolor=edges[segment], linewidth=1, label=segment)
        for i, v in enumerate(values):
            if v >= 10:
                ax.text(left[i] + v / 2, i, f"{v:.0f}%", ha="center", va="center", fontsize=8.5, color=TOKENS["ink"])
        left += values
    ax.set_yticks(y, group_order)
    ax.set_xlim(0, 100)
    ax.xaxis.set_major_formatter(mticker.PercentFormatter())
    ax.set_xlabel("占比")
    ax.set_ylabel("")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), frameon=False, ncol=2, borderaxespad=0)
    add_chart_header(
        fig,
        ax,
        "EPIC-KITCHENS-100 由旧版视频和新增扩展共同构成",
        "视频数量、总时长和原始文件体积均按 700 个 MP4 统计；体积来自远端文件头。",
    )
    charts["source_composition"] = save_fig(fig, "source_composition")

    # 2. Video duration distribution.
    fig, ax = plt.subplots(figsize=(9.4, 4.9))
    sns.histplot(
        data=video_info,
        x="duration_min",
        bins=np.arange(0, 65, 3),
        color=COLOR_FAMILIES["blue"]["base"],
        edgecolor=COLOR_FAMILIES["blue"]["dark"],
        linewidth=1,
        ax=ax,
    )
    med = video_info["duration_min"].median()
    ax.axvline(med, color=TOKENS["ink"], linestyle=":", linewidth=1.2)
    ax.text(med + 0.8, ax.get_ylim()[1] * 0.86, f"中位数 {med:.1f} 分钟", fontsize=9, color=TOKENS["ink"])
    ax.set_xlabel("单个视频时长（分钟）")
    ax.set_ylabel("视频数")
    add_chart_header(
        fig,
        ax,
        "视频以数分钟片段为主，长尾可切成多个会话",
        "700 个视频；均值 8.57 分钟，中位数 5.13 分钟，最长 61.80 分钟。",
    )
    charts["duration_distribution"] = save_fig(fig, "duration_distribution")

    # 3. Participant duration ranking.
    participant = (
        video_info.groupby("participant_id", as_index=False)
        .agg(video_count=("video_id", "count"), duration_h=("duration_h", "sum"), labelled_actions=("labelled_actions", "sum"))
        .sort_values("duration_h", ascending=False)
        .head(15)
        .sort_values("duration_h", ascending=True)
    )
    fig, ax = plt.subplots(figsize=(9.4, 5.7))
    sns.barplot(
        data=participant,
        x="duration_h",
        y="participant_id",
        hue="participant_id",
        palette={pid: COLOR_FAMILIES["olive"]["base"] for pid in participant["participant_id"]},
        dodge=False,
        legend=False,
        edgecolor=COLOR_FAMILIES["olive"]["dark"],
        linewidth=1,
        ax=ax,
    )
    for patch, (_, row) in zip(ax.patches, participant.iterrows()):
        ax.text(
            patch.get_width() + 0.08,
            patch.get_y() + patch.get_height() / 2,
            f"{row['duration_h']:.1f}h / {int(row['video_count'])} 段",
            va="center",
            ha="left",
            fontsize=8.4,
            color=TOKENS["ink"],
        )
    ax.set_xlabel("总时长（小时）")
    ax.set_ylabel("参与者编号")
    ax.set_xlim(0, participant["duration_h"].max() * 1.22)
    add_chart_header(
        fig,
        ax,
        "少数参与者提供了最适合构造多会话记忆流的长序列",
        "按参与者聚合总时长；P04、P22、P01、P02 都超过 6 小时。",
    )
    charts["participant_duration"] = save_fig(fig, "participant_duration")

    # 4. Action split coverage.
    split_df = pd.DataFrame(
        [
            {"split": "训练集动作标签", "segments": len(train)},
            {"split": "验证集动作标签", "segments": len(val)},
            {"split": "测试集时间戳", "segments": len(test)},
        ]
    ).sort_values("segments", ascending=True)
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    palette = {
        "训练集动作标签": COLOR_FAMILIES["blue"]["base"],
        "验证集动作标签": COLOR_FAMILIES["gold"]["base"],
        "测试集时间戳": COLOR_FAMILIES["orange"]["base"],
    }
    sns.barplot(
        data=split_df,
        x="segments",
        y="split",
        hue="split",
        palette=palette,
        legend=False,
        dodge=False,
        ax=ax,
        edgecolor=TOKENS["ink"],
        linewidth=1,
    )
    for patch, value in zip(ax.patches, split_df["segments"]):
        ax.text(patch.get_width() + 600, patch.get_y() + patch.get_height() / 2, fmt_int(value), va="center", fontsize=9)
    ax.set_xlabel("片段数")
    ax.set_ylabel("")
    ax.set_xlim(0, split_df["segments"].max() * 1.18)
    add_chart_header(
        fig,
        ax,
        "89,977 个动作片段覆盖训练、验证和测试时间戳",
        "测试集公开时间戳但不公开动作类别；训练和验证集可用于统计动作/物体分布。",
    )
    charts["split_segments"] = save_fig(fig, "split_segments")

    # 5. Action duration distribution.
    duration = labelled["action_duration_sec"].dropna()
    duration = duration[(duration >= 0) & (duration <= 30)]
    fig, ax = plt.subplots(figsize=(9.4, 4.9))
    sns.histplot(
        x=duration,
        bins=np.arange(0, 31, 1),
        color=COLOR_FAMILIES["pink"]["base"],
        edgecolor=COLOR_FAMILIES["pink"]["dark"],
        linewidth=1,
        ax=ax,
    )
    med_action = duration.median()
    ax.axvline(med_action, color=TOKENS["ink"], linestyle=":", linewidth=1.2)
    ax.text(med_action + 0.5, ax.get_ylim()[1] * 0.84, f"中位数 {med_action:.2f} 秒", fontsize=9, color=TOKENS["ink"])
    ax.set_xlabel("动作片段时长（秒，截断到 30 秒）")
    ax.set_ylabel("动作片段数")
    add_chart_header(
        fig,
        ax,
        "动作标注是细粒度片段，不等同于长期会话边界",
        "训练和验证动作标签；大多数动作持续 1 到 3 秒，适合抽取事件但不能直接替代记忆会话。",
    )
    charts["action_duration"] = save_fig(fig, "action_duration")

    # 6. Top verb classes.
    verb_lookup = verb_classes.set_index("id")["key"].to_dict()
    verb_top = (
        labelled.groupby("verb_class", as_index=False)
        .size()
        .assign(
            label=lambda d: d["verb_class"].map(verb_lookup),
            label_zh=lambda d: d["verb_class"].map(verb_lookup).map(lambda x: VERB_LABEL_ZH.get(x, x)),
        )
        .sort_values("size", ascending=False)
        .head(12)
        .sort_values("size", ascending=True)
    )
    fig, ax = plt.subplots(figsize=(9.4, 5.5))
    sns.barplot(
        data=verb_top,
        x="size",
        y="label_zh",
        hue="label_zh",
        palette={label: COLOR_FAMILIES["orange"]["base"] for label in verb_top["label_zh"]},
        legend=False,
        dodge=False,
        edgecolor=COLOR_FAMILIES["orange"]["dark"],
        linewidth=1,
        ax=ax,
    )
    for patch, value in zip(ax.patches, verb_top["size"]):
        ax.text(patch.get_width() + 250, patch.get_y() + patch.get_height() / 2, fmt_int(value), va="center", fontsize=8.3)
    ax.set_xlabel("动作标签数")
    ax.set_ylabel("动词类别")
    ax.set_xlim(0, verb_top["size"].max() * 1.18)
    add_chart_header(
        fig,
        ax,
        "高频动词集中在拿取、放置、清洗和开合动作",
        "训练和验证集；按官方 97 个动词类别聚合，而非文本变体。",
    )
    charts["top_verbs"] = save_fig(fig, "top_verbs")

    # 7. Top noun classes.
    noun_lookup = noun_classes.set_index("id")["key"].to_dict()
    noun_top = (
        labelled.groupby("noun_class", as_index=False)
        .size()
        .assign(
            label=lambda d: d["noun_class"].map(noun_lookup),
            label_zh=lambda d: d["noun_class"].map(noun_lookup).map(lambda x: NOUN_LABEL_ZH.get(x, x)),
        )
        .sort_values("size", ascending=False)
        .head(12)
        .sort_values("size", ascending=True)
    )
    fig, ax = plt.subplots(figsize=(9.4, 5.5))
    sns.barplot(
        data=noun_top,
        x="size",
        y="label_zh",
        hue="label_zh",
        palette={label: COLOR_FAMILIES["blue"]["base"] for label in noun_top["label_zh"]},
        legend=False,
        dodge=False,
        edgecolor=COLOR_FAMILIES["blue"]["dark"],
        linewidth=1,
        ax=ax,
    )
    for patch, value in zip(ax.patches, noun_top["size"]):
        ax.text(patch.get_width() + 110, patch.get_y() + patch.get_height() / 2, fmt_int(value), va="center", fontsize=8.3)
    ax.set_xlabel("动作标签数")
    ax.set_ylabel("名词类别")
    ax.set_xlim(0, noun_top["size"].max() * 1.18)
    add_chart_header(
        fig,
        ax,
        "高频物体覆盖水龙头、餐具、储物柜和炊具",
        "训练和验证集；按官方 300 个名词类别聚合，观察到 293 个类别。",
    )
    charts["top_nouns"] = save_fig(fig, "top_nouns")

    # 8. Noun category composition.
    noun_category_lookup = noun_classes.set_index("id")["category"].to_dict()
    noun_cat = (
        labelled.assign(noun_category=lambda d: d["noun_class"].map(noun_category_lookup))
        .groupby("noun_category", as_index=False)
        .size()
        .sort_values("size", ascending=False)
        .head(10)
        .sort_values("size", ascending=True)
    )
    noun_cat["noun_category_zh"] = noun_cat["noun_category"].map(lambda x: NOUN_CATEGORY_ZH.get(x, x))
    fig, ax = plt.subplots(figsize=(9.4, 5.2))
    sns.barplot(
        data=noun_cat,
        x="size",
        y="noun_category_zh",
        hue="noun_category_zh",
        palette={label: COLOR_FAMILIES["gold"]["base"] for label in noun_cat["noun_category_zh"]},
        legend=False,
        dodge=False,
        edgecolor=COLOR_FAMILIES["gold"]["dark"],
        linewidth=1,
        ax=ax,
    )
    for patch, value in zip(ax.patches, noun_cat["size"]):
        ax.text(patch.get_width() + 250, patch.get_y() + patch.get_height() / 2, fmt_int(value), va="center", fontsize=8.3)
    ax.set_xlabel("动作标签数")
    ax.set_ylabel("名词上位类别")
    ax.set_xlim(0, noun_cat["size"].max() * 1.18)
    add_chart_header(
        fig,
        ax,
        "物体类别天然偏向厨房记忆：器具、餐具、家电和食材占主导",
        "训练和验证集；名词上位类别来自官方名词类别表。",
    )
    charts["noun_categories"] = save_fig(fig, "noun_categories")

    # 9. Video duration vs action density.
    scatter_df = video_info[video_info["labelled_actions"] > 0].copy()
    fig, ax = plt.subplots(figsize=(9.4, 5.2))
    palette = {
        "EPIC-KITCHENS-55 原有视频": COLOR_FAMILIES["blue"]["base"],
        "EPIC-KITCHENS-100 新增扩展": COLOR_FAMILIES["orange"]["base"],
    }
    sns.scatterplot(
        data=scatter_df,
        x="duration_min",
        y="actions_per_min",
        hue="source_subset",
        palette=palette,
        edgecolor=TOKENS["ink"],
        linewidth=0.35,
        alpha=0.72,
        s=38,
        ax=ax,
    )
    ax.set_xlabel("视频时长（分钟）")
    ax.set_ylabel("每分钟动作标签数")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), frameon=False, ncol=2, borderaxespad=0)
    add_chart_header(
        fig,
        ax,
        "视频长度和动作密度差异较大，首批样本应按参与者和动作覆盖筛选",
        "训练和验证集视频；测试集因无公开动作类别未计入动作密度。",
    )
    charts["duration_density"] = save_fig(fig, "duration_density")

    # 10. Storage footprint.
    storage_df = pd.DataFrame(
        [
            {"item": "原始 MP4", "gb": RAW_TOTAL_GB},
            {"item": "540p 代理视频估计", "gb": PROXY_TOTAL_GB_EST},
            {"item": "540p 代理预算上界", "gb": PROXY_TOTAL_GB_BUDGET_HIGH},
        ]
    ).sort_values("gb", ascending=True)
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    storage_palette = {
        "原始 MP4": COLOR_FAMILIES["orange"]["base"],
        "540p 代理视频估计": COLOR_FAMILIES["blue"]["base"],
        "540p 代理预算上界": COLOR_FAMILIES["olive"]["base"],
    }
    sns.barplot(
        data=storage_df,
        x="gb",
        y="item",
        hue="item",
        palette=storage_palette,
        legend=False,
        dodge=False,
        edgecolor=TOKENS["ink"],
        linewidth=1,
        ax=ax,
    )
    for patch, value in zip(ax.patches, storage_df["gb"]):
        ax.text(patch.get_width() + 25, patch.get_y() + patch.get_height() / 2, f"{value:,.0f} GB", va="center", fontsize=9)
    ax.set_xlabel("存储体积（GB）")
    ax.set_ylabel("")
    ax.set_xlim(0, RAW_TOTAL_GB * 1.14)
    add_chart_header(
        fig,
        ax,
        "全量原始视频约 1.25 TB，代理视频可降到数十 GB",
        "原始体积来自 700 个远端 MP4 文件头；代理体积按 3 个样本 540p 转码外推。",
    )
    charts["storage"] = save_fig(fig, "storage")

    return charts


def table_rows(rows: list[dict[str, object]], columns: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(row.get(key, '')))}</td>" for key, _ in columns)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def chart_figure(charts: dict[str, Path], key: str, caption: str) -> str:
    return (
        f'<figure class="chart"><img src="{image_to_data_uri(charts[key])}" alt="{html.escape(caption)}">'
        f"<figcaption>{html.escape(caption)}</figcaption></figure>"
    )


def sample_image(path: Path, label: str) -> str:
    if not path.exists():
        return ""
    return (
        f'<figure class="sample"><img src="{image_to_data_uri(path)}" alt="{html.escape(label)}">'
        f"<figcaption>{html.escape(label)}</figcaption></figure>"
    )


def build_html(data: dict[str, pd.DataFrame], charts: dict[str, Path]) -> str:
    video_info = data["video_info"]
    labelled = data["labelled"]
    train = data["train"]
    val = data["val"]
    test = data["test"]
    verb_classes = data["verb_classes"]
    noun_classes = data["noun_classes"]
    candidates = data["candidate_participants"].head(8)
    candidate_videos = data["candidate_videos"]

    total_hours = video_info["duration"].sum() / 3600
    total_actions = len(train) + len(val) + len(test)
    observed_verb_classes = labelled["verb_class"].nunique()
    observed_noun_classes = labelled["noun_class"].nunique()
    participants = video_info["participant_id"].nunique()
    median_duration = video_info["duration_min"].median()
    mean_duration = video_info["duration_min"].mean()
    max_duration = video_info["duration_min"].max()
    action_median = labelled["action_duration_sec"].median()
    action_mean = labelled["action_duration_sec"].mean()

    stat_cards = [
        ("视频总数", fmt_int(len(video_info)), "EPIC-KITCHENS-100 元信息表"),
        ("总时长", f"{total_hours:.2f} 小时", "700 个 MP4 片段合计"),
        ("参与者编号", fmt_int(participants), "本地标注表中可见的 participant_id"),
        ("厨房数", "45", "官方主页口径"),
        ("动作片段", fmt_int(total_actions), "训练、验证、测试时间戳合计"),
        ("原始视频体积", "1.25 TB", "700 个远端 MP4 文件头求和"),
        ("动词类别", fmt_int(len(verb_classes)), "官方类别体系"),
        ("名词类别", fmt_int(len(noun_classes)), "官方类别体系"),
    ]
    stat_html = "".join(
        f'<div class="metric"><div class="metric-value">{html.escape(value)}</div>'
        f'<div class="metric-label">{html.escape(label)}</div>'
        f'<div class="metric-note">{html.escape(note)}</div></div>'
        for label, value, note in stat_cards
    )

    candidate_rows = []
    for _, row in candidates.iterrows():
        candidate_rows.append(
            {
                "participant": row["participant_id"],
                "videos": int(row["video_count"]),
                "hours": f"{float(row['total_duration_h']):.2f}",
                "actions": fmt_int(row["labelled_actions"]),
                "verbs": int(row["unique_verb_classes"]),
                "nouns": int(row["unique_noun_classes"]),
            }
        )
    candidate_table = table_rows(
        candidate_rows,
        [
            ("participant", "参与者"),
            ("videos", "视频数"),
            ("hours", "总时长/小时"),
            ("actions", "已标注动作"),
            ("verbs", "动词类别"),
            ("nouns", "名词类别"),
        ],
    )

    downloaded = candidate_videos[candidate_videos["video_id"].isin(["P04_106", "P04_24", "P04_29"])].copy()
    downloaded["source_subset"] = downloaded["video_id"].map(source_subset)
    downloaded_rows = []
    for _, row in downloaded.sort_values("video_id").iterrows():
        downloaded_rows.append(
            {
                "video": row["video_id"],
                "source": row["source_subset"],
                "duration": f"{float(row['duration_min']):.2f}",
                "actions": fmt_int(row["labelled_actions"]),
                "use": SUGGESTED_USE_ZH.get(row["suggested_use"], row["suggested_use"]),
            }
        )
    downloaded_table = table_rows(
        downloaded_rows,
        [
            ("video", "视频"),
            ("source", "来源"),
            ("duration", "时长/分钟"),
            ("actions", "动作标签"),
            ("use", "初筛用途"),
        ],
    )

    contact_dir = Path("data/contact_sheets/P04")
    contact_html = "".join(
        [
            sample_image(contact_dir / "P04_106_contact.jpg", "P04_106：新增扩展视频，约 4.2 分钟"),
            sample_image(contact_dir / "P04_24_contact.jpg", "P04_24：旧版视频纳入 EPIC-KITCHENS-100，约 8.4 分钟"),
            sample_image(contact_dir / "P04_29_contact.jpg", "P04_29：旧版视频纳入 EPIC-KITCHENS-100，约 7.1 分钟"),
        ]
    )

    css = """
:root {
  --paper: #fcfcfd;
  --panel: #ffffff;
  --ink: #1f2430;
  --muted: #667085;
  --line: #e4e7ee;
  --blue: #5477c4;
  --blue-soft: #eaf1fe;
  --orange: #cc6f47;
  --olive: #71b436;
  --gold: #b8a037;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: var(--paper);
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Segoe UI", sans-serif;
  line-height: 1.62;
}
.page {
  max-width: 1120px;
  margin: 0 auto;
  padding: 52px 28px 72px;
  width: 100%;
}
header {
  padding-bottom: 28px;
  border-bottom: 1px solid var(--line);
}
.eyebrow {
  color: var(--blue);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}
h1 {
  margin: 10px 0 12px;
  font-size: 42px;
  line-height: 1.16;
  letter-spacing: 0;
  overflow-wrap: anywhere;
}
.dataset-title {
  display: inline;
}
.subtitle {
  max-width: 860px;
  margin: 0;
  color: var(--muted);
  font-size: 17px;
}
section {
  margin-top: 44px;
}
h2 {
  margin: 0 0 14px;
  font-size: 24px;
  line-height: 1.25;
  letter-spacing: 0;
}
h3 {
  margin: 24px 0 8px;
  font-size: 17px;
}
p {
  margin: 10px 0 0;
}
.summary {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 22px;
}
.summary-item {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px 20px;
  min-width: 0;
}
.summary-item strong {
  display: block;
  margin-bottom: 6px;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-top: 22px;
}
.metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px 16px 14px;
  min-width: 0;
}
.metric-value {
  font-size: 24px;
  font-weight: 760;
  line-height: 1.15;
}
.metric-label {
  margin-top: 6px;
  font-weight: 650;
}
.metric-note {
  margin-top: 5px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.35;
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  align-items: start;
}
.chart, .sample {
  margin: 18px 0 0;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
}
.chart img, .sample img {
  display: block;
  width: 100%;
  height: auto;
  border-radius: 4px;
}
figcaption {
  color: var(--muted);
  font-size: 12.5px;
  margin-top: 8px;
}
.note {
  background: var(--blue-soft);
  border-left: 4px solid var(--blue);
  padding: 14px 16px;
  border-radius: 6px;
  margin-top: 16px;
}
table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 16px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  font-size: 14px;
}
th, td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
th {
  background: #f4f6fa;
  font-weight: 700;
}
tr:last-child td {
  border-bottom: 0;
}
ul {
  padding-left: 20px;
  margin: 10px 0 0;
}
li + li {
  margin-top: 6px;
}
.small {
  color: var(--muted);
  font-size: 13px;
}
.source-list {
  font-size: 13px;
  color: var(--muted);
}
.source-list a {
  color: var(--blue);
}
@media (max-width: 860px) {
  .summary, .metrics, .grid-2 { grid-template-columns: 1fr; }
  h1 { font-size: 28px; line-height: 1.18; }
  .dataset-title { display: block; }
  .page { padding: 34px 18px 54px; }
  .subtitle, .summary-item, .summary-item strong, p, li {
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  table {
    display: block;
    overflow-x: auto;
    white-space: nowrap;
  }
}
"""

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EPIC-KITCHENS-100 数据集报告</title>
  <style>{css}</style>
</head>
<body>
<main class="page">
  <header>
    <div class="eyebrow">数据集技术报告</div>
    <h1><span class="dataset-title">EPIC-KITCHENS-100</span> 数据集报告</h1>
    <p class="subtitle">面向第一人称厨房视频与跨会话视频记忆基准评测的数据概览、标注结构、规模分布和工程成本评估。</p>
  </header>

  <section>
    <h2>技术摘要</h2>
    <div class="summary">
      <div class="summary-item"><strong>数据集适合作为厨房域长期视频记忆的受控子轨道。</strong>它提供同一参与者、同一厨房内多段第一人称视频，场景边界稳定，物体、台面、水槽、炉灶和餐具反复出现，适合构造物体位置、状态变化、流程演进和习惯模式问题。</div>
      <div class="summary-item"><strong>标注是事件级而不是记忆级。</strong>训练和验证集给出动词、名词、起止时间等细粒度动作片段；这些标签适合生成事件表和候选证据，但不能直接替代跨会话问答真值。</div>
      <div class="summary-item"><strong>全量原始视频规模可管理，但不适合放在个人电脑。</strong>700 个 MP4 原始视频约 1.25 TB；按当前 540p 转码设置，全量代理视频预计约 40 GB，实际建议预留 45 到 60 GB。</div>
      <div class="summary-item"><strong>首批样本建议按参与者筛选。</strong>P04、P02、P22、P01、P03 的视频数、总时长和动作覆盖较高，适合先构建 5 到 10 个会话的原型流。</div>
    </div>
    <div class="metrics">{stat_html}</div>
  </section>

  <section>
    <h2>数据范围和组成结构</h2>
    <p>EPIC-KITCHENS-100 是厨房场景中的第一人称视频数据集，由旧版 EPIC-KITCHENS-55 视频和 EPIC-KITCHENS-100 新增扩展视频共同构成。报告中的统计以 EPIC-KITCHENS-100 官方标注仓库为准；旧版视频虽然被纳入新版，但不应混用旧版标注。</p>
    {chart_figure(charts, "source_composition", "数据集组成：旧版视频和新增扩展在数量、时长和原始体积上的占比。")}
    <p class="note">对基准评测构造而言，来源子集主要影响下载路径和潜在训练污染分析；正式样本仍应统一视为 EPIC-KITCHENS-100，并在元数据中保留来源字段。</p>
  </section>

  <section>
    <h2>视频粒度决定了会话构造方式</h2>
    <p>视频粒度整体适合“一个视频作为一个会话”或“长视频切成多个会话”两种策略。中位视频约 5.1 分钟，平均约 8.6 分钟，但存在 30 到 60 分钟的长尾视频，适合切成连续或有时间间隔的会话。</p>
    <div class="grid-2">
      {chart_figure(charts, "duration_distribution", "视频时长分布：多数视频在十几分钟以内，但存在可切分的长尾。")}
      {chart_figure(charts, "participant_duration", "参与者总时长排行：优先选择视频数和总时长都高的参与者。")}
    </div>
  </section>

  <section>
    <h2>动作标注提供事件证据，但需要二次组织成记忆流</h2>
    <p>训练和验证集的动作标签提供了叙述文本、起止时间、动词类别、名词类别和多名词列表。测试集公开时间戳但不公开类别。动作片段中位时长约 {action_median:.2f} 秒，平均约 {action_mean:.2f} 秒，说明它们更适合做事件抽取和证据定位，而不是直接作为长期会话单位。</p>
    <div class="grid-2">
      {chart_figure(charts, "split_segments", "动作片段覆盖：训练/验证标签和测试时间戳共同构成 89,977 个片段。")}
      {chart_figure(charts, "action_duration", "动作片段时长：事件级标注集中在数秒范围。")}
    </div>
  </section>

  <section>
    <h2>动作和物体分布体现厨房域的强结构性</h2>
    <p>高频动词集中在取放、开合、清洗和加热控制；高频名词集中在水龙头、餐具、橱柜、锅具和冰箱。这种分布对普通日常长期记忆代表性有限，但对厨房域的物体状态、位置变化和流程记忆非常有利。</p>
    <div class="grid-2">
      {chart_figure(charts, "top_verbs", "高频动词类别：取放、开合、清洗和开启关闭占据核心。")}
      {chart_figure(charts, "top_nouns", "高频名词类别：水龙头、餐具、储物和炊具是主要观察对象。")}
    </div>
    {chart_figure(charts, "noun_categories", "名词上位类别：厨房器具、餐具、家电和食材构成主要记忆对象。")}
  </section>

  <section>
    <h2>样本筛选应兼顾总时长、动作密度和物体覆盖</h2>
    <p>同一参与者内部的视频差异明显，过短或动作稀疏的视频不适合作为首批基准评测样本。首轮应优先选择总时长高、动作覆盖足、物体类别丰富且场景稳定的参与者。</p>
    {chart_figure(charts, "duration_density", "视频时长与动作密度：动作密度差异较大，需要按视频和参与者做二次筛选。")}
    <h3>首批候选参与者</h3>
    {candidate_table}
  </section>

  <section>
    <h2>P04 小样本说明</h2>
    <p>当前已在服务器下载并转码了 P04 的 3 个视频。它们覆盖同一厨房中食材处理、烹饪、清洗和物体回收等操作，适合用来验证跨会话记忆问题是否成立。</p>
    {downloaded_table}
    <div class="grid-2">{contact_html}</div>
  </section>

  <section>
    <h2>存储和预处理成本</h2>
    <p>全量原始视频约 1.25 TB，不建议保存在个人电脑。当前 3 个样本原始视频合计 4.26 GB，占全量原始体积约 0.34%；转成 540p、25 fps、H.264 代理视频后合计 131.94 MB。按这个设置外推，全量代理视频约 40 GB，实际预留 45 到 60 GB 更稳妥。</p>
    {chart_figure(charts, "storage", "存储估算：原始视频和 540p 代理视频的量级差异。")}
  </section>

  <section>
    <h2>对跨会话视频记忆基准评测的启示</h2>
    <ul>
      <li><strong>推荐使用方式：</strong>以参与者为单位构建记忆流，再把每个视频或视频切片作为会话。</li>
      <li><strong>适合题型：</strong>物体位置变化、最终状态、食材处理阶段、工具复用、清洗/收纳流程、同一厨房固定布局记忆。</li>
      <li><strong>不适合直接宣称：</strong>完整日常生活长期记忆。该数据集是厨房域，场景集中，应该作为厨房记忆子任务。</li>
      <li><strong>标注策略：</strong>用官方动作标签做事件候选，用视觉语言模型生成结构化事件表，再由人工校验跨会话问题和证据时间戳。</li>
    </ul>
  </section>

  <section>
    <h2>限制和待验证问题</h2>
    <ul>
      <li>公开标注表没有直接给出真实日期和跨天间隔，跨天长期记忆需要结合原始采集信息或视频内容进一步判断。</li>
      <li>训练/验证动作标签是细粒度操作标签，不覆盖所有可见状态变化；物体最终位置和状态仍需要视觉检查或二次标注。</li>
      <li>部分视频来自旧版 EPIC-KITCHENS-55，部分来自 EPIC-KITCHENS-100 扩展；做模型评测时需要记录来源，避免训练污染分析不清。</li>
      <li>厨房域强结构性是优势也是限制：它适合受控记忆评测，但不能代表 Ego4D 那种更广泛的日常生活场景。</li>
    </ul>
  </section>

  <section>
    <h2>建议的下一步</h2>
    <ul>
      <li>先围绕 P04 构建 5 到 10 个会话的最小原型，人工设计 20 到 50 个跨会话问题。</li>
      <li>批量下载 P04、P02、P22、P01、P03 的候选视频，统一转码为 540p 代理视频并生成联系表。</li>
      <li>建立结构化事件表，字段至少包括会话编号、时间段、地点、可见物体、动作、状态变化、证据帧和置信度。</li>
      <li>将问题分为持久事实、状态更新、时间演进、频率/最近性和跨会话一致性五类，并标注所需会话。</li>
    </ul>
  </section>

  <section>
    <h2>资料来源和复现路径</h2>
    <p class="source-list">主要来源包括：EPIC-KITCHENS 官方主页、EPIC-KITCHENS-100 标注仓库、本地下载脚本清单、远端 MP4 文件头统计，以及本地生成的元信息表。核心脚本为 <code>scripts/analyze_epic_kitchens_100.py</code> 和 <code>scripts/build_epic_kitchens_report.py</code>。官方主页：<a href="https://epic-kitchens.github.io/">https://epic-kitchens.github.io/</a>；标注仓库：<a href="https://github.com/epic-kitchens/epic-kitchens-100-annotations">https://github.com/epic-kitchens/epic-kitchens-100-annotations</a>。</p>
    <p class="small">生成时间：2026-06-30。报告中的大小单位 GB 使用十进制；GiB 换算见存储估算附表。</p>
  </section>
</main>
</body>
</html>
"""
    return html_doc


def write_source_notes(charts: dict[str, Path]) -> None:
    chart_lines = [
        "# EPIC-KITCHENS-100 HTML 报告源说明",
        "",
        "## 报告结构映射",
        "",
        "- 技术摘要：对应报告核心结论。",
        "- 数据范围和组成结构、视频粒度、动作标注、动作和物体分布、筛选建议：对应带图表证据的主要发现。",
        "- 数据范围、资料来源、复现路径：对应数据范围、来源和指标口径。",
        "- 存储和预处理成本、限制和待验证问题：对应方法说明和局限性。",
        "- 建议的下一步：对应后续工作建议与待回答问题。",
        "",
        "## 图表地图",
        "",
    ]
    descriptions = {
        "source_composition": "组成：按来源比较数量、时长、体积占比；形式为 100% 堆叠横条。",
        "duration_distribution": "时长分布：700 个视频时长直方图。",
        "participant_duration": "参与者排行：按总视频时长排序的前 15 名。",
        "split_segments": "动作片段覆盖：训练、验证、测试时间戳数量。",
        "action_duration": "动作时长分布：训练/验证动作片段，截断到 30 秒。",
        "top_verbs": "动词长尾：训练/验证中最高频的官方动词类别。",
        "top_nouns": "名词长尾：训练/验证中最高频的官方名词类别。",
        "noun_categories": "名词上位类别：训练/验证动作标签关联的名词上位类别。",
        "duration_density": "筛选关系：视频时长与动作密度散点。",
        "storage": "存储估算：原始视频和代理视频量级比较。",
    }
    for key, path in charts.items():
        chart_lines.append(f"- `{key}`：{descriptions.get(key, '')} 输出 `{path}`。")
    chart_lines.extend(
        [
            "",
            "## 关键口径",
            "",
            "- 原始视频总大小来自服务器对 700 个远端 MP4 文件头的 `Content-Length` 求和。",
            "- 540p 全量代理体积按 P04 三个样本的 540p 转码大小外推。",
            "- 测试集只有公开时间戳，不含公开动词/名词标签，因此动作/物体分布只使用训练和验证集。",
            "- 旧版 EPIC-KITCHENS-55 视频被纳入 EPIC-KITCHENS-100，但报告统一使用 EPIC-KITCHENS-100 标注体系。",
        ]
    )
    SOURCE_NOTES_PATH.write_text("\n".join(chart_lines) + "\n", encoding="utf-8")


def main() -> None:
    if not ROOT.exists():
        raise SystemExit(f"Missing annotation directory: {ROOT}")
    if not DOWNLOAD.exists():
        raise SystemExit(f"Missing download script directory: {DOWNLOAD}")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    data = load_data()
    charts = make_charts(data)
    REPORT_PATH.write_text(build_html(data, charts), encoding="utf-8")
    write_source_notes(charts)
    print(f"Wrote {REPORT_PATH}")
    print(f"Wrote {SOURCE_NOTES_PATH}")


if __name__ == "__main__":
    main()
