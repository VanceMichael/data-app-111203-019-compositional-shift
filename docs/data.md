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
