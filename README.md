# 多架构镜像发布页

CI 的 amd64 / arm64 镜像分批推入本地 OCI Image Layout 后，运维在网页上为一个
**已有标签**选取两份清单，系统核对两份镜像配置中的架构与
`org.opencontainers.image.revision`（同一源码提交），并对清单及其引用的全部
blob（config + layers）做**分块**大小与摘要校验。全部通过后才生成多架构索引并
原子地拨动标签；任何一步失败都保留现役标签。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

首次启动时容器会在数据卷中生成样例 layout（`demo` 仓库）：

| 标签 | 说明 |
| --- | --- |
| `demo:latest` | 现役标签，指向 v1.0.0 的多架构索引（revision A） |
| `demo:1.1.0-amd64` / `demo:1.1.0-arm64` | 同一提交（revision B），可用于一次正常发布 |
| `demo:1.2.0-amd64` / `demo:1.2.0-arm64` | **故意**来自不同提交，用于演示同提交核对失败 |

本地运行（无 Docker）：

```bash
pip install -r requirements.txt
python tools/make_sample_layout.py /tmp/layout --if-empty
python -m app.server --layout /tmp/layout --port 8000
```

## 使用流程

1. 打开页面，"标签现状" 列出所有标签的当前摘要、类型、架构与 revision；
   点 "查看索引/清单" 可查看当前索引 JSON 及两架构清单的摘要。
2. 在 "发起发布" 选择目标标签与 amd64 / arm64 两份清单。表单下方显示
   **提交时期望的标签摘要**（即选清单时所见）。
3. 点 "校验并发布"。发布在后台异步进行，"发布记录" 实时显示进度
   （已校验文件数 / 当前 blob 字节进度）。验层期间现役标签照常可查，
   其他发布照常提交。
4. 结果：
   - **完成**：标签已指向新索引（两架构同提交、全部 blob 校验通过）。
   - **失败**：缺层 / 摘要或大小不符 / 架构不符 / 两架构不同提交等，
     现役标签不变。
   - **冲突·需刷新**：提交时标签摘要与选择时所见不同（期间有人抢先发布），
     现役标签保持为他人发布的结果，页面提示刷新。

## 实现要点

```
app/oci.py        OCI layout 读写原语
app/releases.py   后台发布流水线（验层 → 同提交核对 → 建索引 → 拨标签）
app/server.py     Flask 路由（页面 + JSON API）
app/sampledata.py 样例 layout 生成（demo 仓库，含故意的不同提交对）
tools/make_sample_layout.py  样例生成 CLI（compose 启动时调用）
tests/test_release.py        端到端测试
```

- **分块校验**：`OciLayout.verify_blob` 以 1 MiB 块流式读取 blob，累计
  sha256 与字节数，与描述中的 digest / size 比对；清单本体、config、每个
  layer 都校验，进度写入任务供页面轮询。
- **同提交核对**：架构取镜像 config 的 `architecture`，revision 取 config
  label `org.opencontainers.image.revision`（缺失时回退到清单同名注解）；
  两份清单 revision 必须非空且相等。
- **失败保留现役标签**：校验、建索引、写索引 blob 全部完成后才触碰
  `index.json`；blob 与 `index.json` 都走临时文件 + `os.replace` 原子替换，
  任何前置失败都不会改动现役标签。
- **乐观并发**：提交时在索引锁内重读 `index.json`，比较标签当前摘要与
  `expected_tag_digest`（选清单时所见），不一致则拒绝并提示刷新；
  验层阶段不持锁，因此现役标签可查、其他发布可并行提交，只有最后的
  拨标签被串行化。
- **索引产物**：`application/vnd.oci.image.index.v1+json`，两条
  descriptor 各带 `platform: {architecture, os}`，索引注解写入 revision。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 发布页 |
| GET | `/api/overview` | 标签现状 + 候选清单（架构 / revision / 关联标签） |
| GET | `/api/tags/<name>` | 标签当前描述符；索引标签附索引 JSON 与两架构清单摘要 |
| POST | `/api/releases` | `{tag, amd64, arm64, expected_tag_digest}` → 202 + 任务 |
| GET | `/api/releases` | 任务列表（状态 / 进度 / 结果） |
| GET | `/api/releases/<id>` | 单个任务详情 |

## 测试与实测结果

```bash
python -m pytest tests/ -v
```

11 个端到端测试全部通过（本机 Python 3.11 + Flask 3.1，2.6s）：

- 页面与概览 / 标签详情接口（索引 JSON + 两架构清单摘要）
- 正常发布：同提交两份清单 → 标签指向新索引，索引 blob 自身通过校验，
  其他标签不受影响
- 缺层（删除 blob 文件）→ 失败，现役标签不变
- 层损坏（改一字节）→ `digest_mismatch`，现役标签不变
- 两架构不同提交（样例 1.2.0 对）→ 失败，现役标签不变
- 架构选反 → 失败，现役标签不变
- **交错发布**：任务 A 验层完成后在提交前被挂起，期间确认现役标签可查、
  另一标签的发布照常完成；随后模拟他人抢先拨动 `demo:latest`，A 提交时
  得到"冲突·需刷新"，标签保持为抢先者的值
- 过期 `expected_tag_digest`（无并发）→ 同样冲突拒绝

另用真实服务（`python -m app.server`）+ curl 手工走通：生成样例 → 概览 →
发布 `demo:latest`（8 个文件分块校验）→ 标签详情确认新索引与两架构摘要 →
用旧摘要再次提交 → 收到"请刷新后重试"冲突提示。

## 未完成项 / 限制

- 本环境无 Docker，`docker compose up` 未实际构建运行；Dockerfile 中的
  启动命令（样例生成 + 起服务）已在宿主机逐条实测通过。
- 使用 Flask 内置服务器（threaded），面向内网工具定位；生产部署建议换
  gunicorn / waitress。
- 无认证鉴权，需放在受信网络或反向代理之后。
- 候选清单只枚举 `index.json` 顶层的 image manifest（未递归索引内嵌清单）；
  发布历史仅存于进程内存，重启即清空。
- 孤儿 blob（如冲突后未引用的索引）暂不回收。
