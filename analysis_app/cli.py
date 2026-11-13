import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .composition import Config, analyze
from .io import load_records
from .summary import summarize


def _fractions(text):
    try:
        return tuple(float(item) for item in text.split(',') if item.strip())
    except ValueError:
        raise argparse.ArgumentTypeError('敏感性比例必须是逗号分隔的数值') from None


def save_json(path, result):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.analysis-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description='本地观测数据分析')
    commands = parser.add_subparsers(dest='command', required=True)
    summary = commands.add_parser('summary', help='按业务维度核对观测口径')
    summary.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    summary.add_argument('--output', type=Path)
    composition = commands.add_parser('composition', help='两期组成分布比较')
    composition.add_argument('--input', type=Path,
                             default=Path('data/observations.csv'))
    composition.add_argument('--output', type=Path)
    composition.add_argument('--zero-policy', choices=('detection_limit', 'structural', 'reject'),
                             default='detection_limit',
                             help='零值成因声明：检测限以下 / 结构性不存在 / 拒绝零值')
    composition.add_argument('--zero-fraction', type=float, default=0.5,
                             help='检测限替换比例，取 (0, 1]')
    composition.add_argument('--zero-fractions', type=_fractions,
                             default=(0.1, 0.25, 0.5, 0.65, 1.0),
                             help='敏感性比较的替换比例列表，逗号分隔')
    composition.add_argument('--closure-tolerance', type=float, default=1e-6,
                             help='闭合容差（绝对误差）')
    composition.add_argument('--permutations', type=int, default=999,
                             help='置换检验重采样次数')
    composition.add_argument('--seed', type=int, default=20260101,
                             help='置换检验随机种子')
    composition.add_argument('--alpha', type=float, default=0.05,
                             help='判定阈值')
    composition.add_argument('--baseline', type=int, help='基线比较期')
    composition.add_argument('--current', type=int, help='当前比较期')
    args = parser.parse_args()
    try:
        if args.command == 'summary':
            result = summarize(load_records(args.input))
        else:
            config = Config(closure_tolerance=args.closure_tolerance,
                            zero_policy=args.zero_policy,
                            zero_fraction=args.zero_fraction,
                            sensitivity_fractions=args.zero_fractions,
                            permutations=args.permutations,
                            seed=args.seed,
                            alpha=args.alpha,
                            baseline_period=args.baseline,
                            current_period=args.current)
            result = analyze(load_records(args.input), config)
        if args.output:
            save_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
