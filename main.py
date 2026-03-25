import json
import sys
import argparse
from pathlib import Path


def get_bbox_bounds(bbox):
    """4点のboundingBoxからmin/max座標を返す。"""
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    return min(xs), min(ys), max(xs), max(ys)


def px_per_char(text, bounds, is_vertical):
    """ブロック1文字あたりのピクセル数を返す。文字がない場合は None。"""
    n = len(text)
    if n == 0:
        return None
    # 縦書き: テキスト方向はy軸、横書き: x軸
    size = (bounds[3] - bounds[1]) if is_vertical else (bounds[2] - bounds[0])
    return size / n


def layout_box_to_text(blocks, threshold=1.0, blank_line_ratio=3.0):
    """
    1つのレイアウトボックス内のテキストブロック列をプレインテキストに変換する。

    縦書きの場合、ブロックの末端y座標がレイアウトボックスの底辺に近ければ
    次のブロックと文字列を結合する（改行しない）。
    横書きの場合は末端x座標で同様の判定を行う。

    threshold: 端判定の許容幅を「文字数」で指定（デフォルト1.0文字分）。
      ブロックのバウンディングボックスサイズ÷文字数で1文字あたりのpxを算出し、
      threshold * px_per_char 以上の余白があれば端ではないと判断する。

    隣接する行間の間隔が通常より blank_line_ratio 倍以上広い場合は空行を挿入する。
    """
    if not blocks:
        return "", False

    # 縦書き判定（多数決）
    is_vertical = (
        sum(1 for b in blocks if b.get("isVertical") == "true") > len(blocks) / 2
    )

    # レイアウトボックス全体の「端」座標を求める
    all_bounds = [get_bbox_bounds(b["boundingBox"]) for b in blocks]
    if is_vertical:
        layout_edge = max(b[3] for b in all_bounds)  # 最大 y
    else:
        layout_edge = max(b[2] for b in all_bounds)  # 最大 x

    # ブロックを「行」単位にグループ化（端で切れたブロックは次と結合）
    line_groups = []  # [(text, [bounds, ...]), ...]
    current_text = ""
    current_bounds = []

    for block, bounds in zip(blocks, all_bounds):
        text = block.get("text", "")
        current_text += text
        current_bounds.append(bounds)

        block_edge = bounds[3] if is_vertical else bounds[2]
        ppc = px_per_char(text, bounds, is_vertical)
        px_threshold = threshold * ppc if ppc else threshold * 10
        at_edge = (layout_edge - block_edge) <= px_threshold

        if not at_edge:
            line_groups.append((current_text, current_bounds))
            current_text = ""
            current_bounds = []

    last_at_edge = bool(current_text)  # ループ後に残っていれば末尾が端で切れている
    if current_text:
        line_groups.append((current_text, current_bounds))

    if len(line_groups) <= 1:
        return "".join(t for t, _ in line_groups), last_at_edge

    # 隣接行間のギャップを計算
    # 縦書き: x方向（右→左）、横書き: y方向（上→下）
    gaps = []
    for i in range(len(line_groups) - 1):
        _, bounds_curr = line_groups[i]
        _, bounds_next = line_groups[i + 1]
        if is_vertical:
            curr_edge = min(b[0] for b in bounds_curr)   # 現在行の左端
            next_edge = max(b[2] for b in bounds_next)   # 次行の右端
        else:
            curr_edge = min(b[1] for b in bounds_curr)   # 現在行の上端
            next_edge = max(b[3] for b in bounds_next)   # 次行の下端
        gaps.append(max(0, curr_edge - next_edge))

    # 典型的なギャップ（中央値）を基準にする
    sorted_gaps = sorted(g for g in gaps if g > 0)
    if sorted_gaps:
        median_gap = sorted_gaps[len(sorted_gaps) // 2]
    else:
        median_gap = 1

    # 行を組み立て、大きなギャップの箇所に空行を挿入
    output_lines = []
    for i, (text, _) in enumerate(line_groups):
        output_lines.append(text)
        if i < len(gaps) and gaps[i] > median_gap * blank_line_ratio:
            output_lines.append("")

    return "\n".join(output_lines), last_at_edge


def json_to_text(path, threshold=1.0):
    """OCR結果JSONファイルをプレインテキストに変換する。
    戻り値: (text, ends_at_edge) — ends_at_edge は末尾ブロックがページ端で切れているか否か。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    parts = []
    last_at_edge = False
    for layout_box in data.get("contents", []):
        text, last_at_edge = layout_box_to_text(layout_box, threshold)
        if text:
            parts.append(text)

    return "\n".join(parts), last_at_edge


def main():
    parser = argparse.ArgumentParser(
        description="NDL OCR JSON をプレインテキストに変換する"
    )
    parser.add_argument("input", help="入力JSONファイルまたはディレクトリ")
    parser.add_argument("-o", "--output", help="出力ファイル（省略時は標準出力）")
    parser.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help="端判定の許容幅（文字数で指定、デフォルト: 1.0）",
    )
    args = parser.parse_args()

    input_path = Path(args.input)

    if input_path.is_file():
        text, _ = json_to_text(input_path, args.threshold)
        output = text
    elif input_path.is_dir():
        json_files = sorted(input_path.glob("*.json"))
        if not json_files:
            print(f"JSONファイルが見つかりません: {input_path}", file=sys.stderr)
            sys.exit(1)
        output_parts = []
        prev_at_edge = False
        for f in json_files:
            text, ends_at_edge = json_to_text(f, args.threshold)
            if not text:
                prev_at_edge = False  # 空ページはページ区切りとして扱う
                continue
            if output_parts:
                # 前ページ末尾が端で切れていれば改行なしで結合、そうでなければページ区切り
                sep = "" if prev_at_edge else "\n\n"
                output_parts.append(sep)
            output_parts.append(text)
            prev_at_edge = ends_at_edge
        output = "".join(output_parts)
    else:
        print(f"エラー: {args.input} は有効なファイルまたはディレクトリではありません", file=sys.stderr)
        sys.exit(1)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"出力しました: {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
