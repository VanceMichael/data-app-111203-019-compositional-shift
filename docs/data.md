# 数据契约

原料分析按供应方汇总三种组分份额、批量和组成闭合误差。

主测量 `component_a` 的单位为质量份额，主体字段是 `batch`，核算维度是 `supplier`。`observation_id` 是文件内唯一观测编号，主体允许重复观测。数值只接受有限值；空单元格解析为 None，不能直接当成零。额外列不进入当前核算。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `observation_id` | str | 必填 |
| `batch` | str | 必填 |
| `supplier` | str | 必填 |
| `period` | int | 必填 |
| `component_a` | float | 必填 |
| `component_b` | float | 必填 |
| `component_c` | float | 必填 |
| `lot_size` | float | 必填 |
| `detection_limit` | float | 必填 |

时间字段 day、week、tick、hour、minute、cycle、start_day、reported_day、known_at、visible_at、opened、observed 均以各字段名所述的相对时间单位表示整数；不同字段并不必然代表同一种时间。缺少的登记码与浓度等信息保持为空。示例只表达产品数据结构，不能代替实际调查资料。

## 业务字段语义

component_a、component_b、component_c 为质量份额，其和在数值容差内为一。lot_size 为吨，detection_limit 是单组分检出限份额。period 为比较期，supplier 是供应方；零份额的原因需由分析配置说明。

## 组成比较约定

- 非负与闭合容差先验证：负值、全零批次、闭合误差超容差、lot_size 或 detection_limit 非正、批次跨供应方/比较期的记录按原因剔除并计数，不进入比较。
- 零值成因由 `--zero-policy` 显式声明：`detection_limit` 视为检测限以下，按 `zero_fraction × detection_limit` 乘法替换（保持闭合与正值组分的相互比值），并对 `--zero-fractions` 列表做敏感性比较；`structural` 视为结构性不存在，对应组分从所有记录的子组成中剔除后再取 CLR；`reject` 遇零即失败。任何策略都不会统一加一个小数再取对数。
- 距离为 CLR 表示下的 Aitchison 距离；置换检验以批次为单元，within_supplier 检验只在 supplier 内部打乱期间标签。
- 每批等权（equal_batch）与 lot_size 物质量加权（mass）是两个独立统计总体，分块输出。
- 分解恒等式：总位移 = 供应方内部配比 + 供应结构 + 不可分离（进入/退出供应方），残差范数随结果给出；没有共同供应方时 within/structure 为 null，全部变化列为不可分离。
- 输出中的 null 表示缺少共同供应方或数据不足，不是零；所有结论均为质量监测线索，不是不合格判定。
