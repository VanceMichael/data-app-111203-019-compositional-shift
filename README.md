# compositional-shift

原料分析按供应方汇总三种组分份额、批量和组成闭合误差，并在闭合约束下比较两个 period 的组成分布。

## 使用

运行需要 Python 3.11 或更新版本，没有第三方运行依赖。数据在本地处理，不请求外部接口。

```bash
python3 -m analysis_app summary
python3 -m analysis_app summary --input data/observations.csv --output output/summary.json
python3 -m analysis_app composition --output output/composition.json
python3 -m analysis_app composition --zero-policy structural --baseline 0 --current 1
```

`summary` 按供应方核对观测口径；`composition` 做两期组成分布比较：

- 先验证非负、闭合容差（`--closure-tolerance`，默认 1e-6）与批次完整性，不通过的行按原因剔除并计数。
- 零值必须声明成因：`--zero-policy detection_limit`（检测限以下，按 `--zero-fraction`×detection_limit 乘法替换保持闭合，并按 `--zero-fractions` 做敏感性比较）、`structural`（结构性不存在，剔除该组分分析子组成）、`reject`（拒绝零值）。不会统一加一个小数再取对数。
- 用 CLR 表示与 Aitchison 距离计算两期距离，置换检验（`--permutations`、`--seed`）以批次为单元重采样，保留 supplier 内批次结构。
- 每批等权与 lot_size 物质量加权分块输出，不合并为同一统计总体。
- 分解给出供应结构变动与同供应方内部配比变动；进入/退出供应方的贡献列为不可分离部分。
- 组分比变化以 log2 倍变化输出；CLR 坐标正负不能读作某种材料绝对增加。

标准输出是完整 JSON，输出文件使用临时文件原子替换；失败退出码为 2，不覆盖既有结果。空数据集保留零计数与空分组，缺失数值不进入数值分母。数据指纹由按 observation_id 排序后的解析记录计算。组成比较结果是质量监测线索，不是不合格判定。

## 验证与容器

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q analysis_app
docker build -t compositional-shift .
docker run --rm compositional-shift
```

容器内也可执行 `python -m unittest discover -s tests -v`。镜像构建阶段取得 Python 镜像，产品启动、测试和演示均无需下载。小规模样例的完整单元测试和 summary 可以连续执行，不需要等待定时任务。

## 代码与数据

- `analysis_app/io.py`：有类型的 CSV 读取、完整性校验及内容指纹。
- `analysis_app/schema.py`：字段类型、主体与聚合维度。
- `analysis_app/domain.py`：当前业务核算函数。
- `analysis_app/summary.py`：确定性的分组与数值口径。
- `analysis_app/composition.py`：两期组成分布比较、零值策略与分解。
- `analysis_app/cli.py`：本地命令及原子输出。
- `docs/data.md`：数据契约。
- `docs/review.md`：演示数据的复盘结论。

演示数据全部为本地合成，不对应现实机构、设备或人员；不能用来推断真实业务效果。
