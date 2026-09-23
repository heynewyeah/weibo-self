# 新模型接口测试（llm-beixian 网关 / qwen36-35b-a3b-fp8）

本目录集中存放**新模型接口**（KServe 网关）相关测试，覆盖：请求参数是否正确、接口是否连通、具体样例结果是否符合预期、以及**与旧模型结果是否一致**。

当前接口（`config/config.yaml` 的 `api.url`）：

```
http://llm-beixian.multimedia.wml.weibo.com/mm-wb-ads/qwen36-35b-a3b-ads-fst-6ab34420/v2/models/llm/chat/completions
```

| 项 | 说明 |
| --- | --- |
| 协议 | KServe v2（`.../v2/models/llm` 返回模型元信息） |
| 服务端模型名 | `qwen36-35b-a3b-fp8`（请求体 `model` 会被网关覆盖） |
| 关闭思考 | `thinking: {"type": "disabled"}`（实测有效；顶层 `enable_thinking: false` 也可） |
| 网关不支持 | `reasoning` 字典、`chat_template_kwargs` 字典（返回 400，只接受 int/bool/string） |
| 旧直连备份 | `config.yaml` 里已注释保留（`:8087/v1/chat/completions`）；切回旧接口时**必须**同时放开 `reasoning: {effort: "none"}` 与 `enable_thinking: false`，否则旧接口会输出思考过程并被 `max_tokens` 截断 |

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `test_model_cases.py` | **具体样例测试**：逐条断言请求成功 / 结果可解析 / 与预期一致；可 `--legacy-url` 与旧模型逐条对比；可 `--task-id` 用 MySQL 真实数据对比 |
| `test_api_client_endpoints.py` | **参数与连通性单元测试**：离线校验请求体构造；`--live` 真实调用校验连通性、思考关闭、多模态 |
| `compare_llm_endpoints.py` | **接口对比工具**：参数矩阵、关思考参数探测、模型元信息、分类一致性、转发审查一致性 |
| `cases/sample_cases.json` | 内置样例（4 条汽车分类 + 2 条高管转发审查） |

## 运行方式

```bash
cd intent_behavior

# 1) 具体样例测试（内置用例，联网即可）
python3 tests/new_request_modal_test/test_model_cases.py

# 2) 具体样例 + 与原模型逐条对比（原模型结果一致性）
python3 tests/new_request_modal_test/test_model_cases.py \
    --legacy-url http://<旧直连地址>:8087/v1/chat/completions \
    --legacy-model /data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B

# 3) 用 MySQL 真实数据对比（以库中 level 当作原模型结果；只在能连库的机器跑）
python3 tests/new_request_modal_test/test_model_cases.py --task-id 1302305683722469377 --limit 10

# 4) 参数 / 连通性单测
python3 -m unittest tests.new_request_modal_test.test_api_client_endpoints -v
python3 tests/new_request_modal_test/test_api_client_endpoints.py --live --check-legacy

# 5) 完整对比（参数矩阵 + 模型信息 + 分类/转发一致性）
python3 tests/new_request_modal_test/compare_llm_endpoints.py --samples 10
```

退出码：`0` 全部通过，`1` 有失败用例，可直接接 CI / 上线前检查。

## 如何加用例

编辑 `cases/sample_cases.json`（或另建文件用 `--cases` 指定），支持两类：

```json
[
  {
    "name": "汽车-新车发布-认知层",
    "type": "classify",
    "industry": "汽车",
    "brand_terms": "蔚来",
    "author_name": "蔚来",
    "content": "博文正文……",
    "expected_layer": "认知层"
  },
  {
    "name": "转发-高管正向宣传-正常",
    "type": "forward_review",
    "industry": "汽车",
    "brand_terms": "蔚来",
    "author_name": "蔚来马麟",
    "content": "转发者自己写的文字……",
    "forward_content": "被转发的原博正文……",
    "expected_verdict": "正常"
  }
]
```

- `type=classify`：预期字段是 `expected_layer`（认知层 / 兴趣层 / 考虑层 / 其他）。
- `type=forward_review`：预期字段是 `expected_verdict`（正常 / 异常）。
- `expected_*` 留空时只校验“请求成功 + 输出可解析”，不校验具体值。
- `--only 关键字` 可以只跑名称中包含关键字的用例。

## MySQL 数据说明

`--task-id` 模式会读取该任务下**已被回写过 level（level≠0）的纯文本记录**，把库里的 level 反查成层级当作“原模型结果”，再和新模型输出对比。数据库连接信息取自 `config/config.yaml` 的 `mysql` 段，可按需改成自己的环境（只读查询，不写库、不回写）。

媒体类（含图片/视频）博文不在该模式覆盖范围内，因为需要先走 mid 反解；如需覆盖，可在 `cases/*.json` 中直接补文本样例。
