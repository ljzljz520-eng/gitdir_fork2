# 逐目标事务下载（staging → 校验 → fsync → 原子发布 → 崩溃恢复）实施计划

## 仓库调研结论

- 项目是单文件脚本 [gitdir.py](file:///Users/kkcarrot/swe-project/gitdir_fork2/gitdir/gitdir.py)（178 行），用 GitHub contents API 递归枚举目录，`urllib.request.urlretrieve(url, path)` **直接写最终路径**，`os.makedirs(exist_ok=True)` 建目录，CTRL+C 时 `sys.exit()`。
- 现状缺陷（与本需求直接相关）：
  1. 写入过程中崩溃 / Ctrl+C 会在最终位置留下半成品文件；重新运行直接覆盖，无任何恢复判定；
  2. 无校验和、无 fsync、无并发协调；两个进程可同时写同一目标；
  3. 目标已存在时天然静默覆盖（当前“既有目标覆盖策略”）。
- GitHub contents API 对每个 blob 提供不可变标识：`sha`（git blob SHA-1，即 `sha1("blob <size>\0"+content)`）、`size`、`download_url`、`path`，可直接作为源计划的校验依据。
- 运行环境 Python 3.9（本机），需同时兼容 Linux / macOS / Windows；仅依赖标准库 + 已有 colorama，不引入新依赖。

## 需求拆解（契约边界）

- **事务只管“尚不存在的目标”**：开始前检查最终目标是否已存在；已存在则走旧的直接下载路径（覆盖策略与本契约分离，不实现覆盖确认弹窗）。`--flatten` 模式同样不进入事务（目标是已存在的共享 output 目录）。
- 每个顶层 URL = 一个“目标（target）”，对应一个事务；事务之间相互独立（逐目标事务）。
- 暂存目录是最终目标的**同级目录**，因此必然同文件系统；仍显式校验 `st_dev` 一致。
- 恢复三动作（继续 / 清理 / 仅报告）**仅在请求的源清单与日志中的不可变清单匹配时**可执行变更动作；不匹配一律拒绝并报告，绝不允许中断作业演变成静默覆盖。

## 文件与模块

- `gitdir/transaction.py`（**新建**，网络无关的事务引擎，约 350–450 行）：
  - 暂存目录布局、清单与哈希链日志、跨平台文件锁、fsync/持久化顺序、原子发布（NOREPLACE 语义）、启动恢复三动作。
- [gitdir/gitdir.py](file:///Users/kkcarrot/swe-project/gitdir_fork2/gitdir/gitdir.py)（**修改**）：
  - 把递归 API 遍历改为“先枚举完整源计划（manifest），再交给事务引擎执行”；提供下载回调；CLI 增加恢复选项；目标已存在 / flatten 时回退旧流程并打印说明。
- `gitdir/tests/test_transaction.py`（**新建**，stdlib `unittest`，无新依赖）：
  - 用本地文件源（不走网络）覆盖崩溃窗口、日志撕裂、清单不匹配拒绝、并发、跨文件系统拒绝等场景。

## 暂存目录布局与命名

最终目标：`<parent>/<target>`（tree URL 即 `<output_dir>/<download_dirs>`；blob 单文件 URL 即 `<output_dir>/<dirname>`，内含该文件）。

同级暂存目录名按目标绝对路径确定性派生，使协作进程收敛到同一事务目录：

```
<parent>/.gitdir-tx-<sha256(abspath(target))[:16]]/
    manifest.json     # 不可变源计划（canonical JSON，按 path 排序）
    tx.journal       # 仅追加、哈希链校验的日志
    lock             # 跨进程文件锁（POSIX fcntl.flock / Windows msvcrt.locking）
    root/…           # 与最终目标完全一致的树（验证过的文件写在这里）
```

- 暂存目录用 `os.mkdir`（原子）创建：并发时只有一个赢家，其余进程转而开锁、读日志判定状态。
- 只重命名 `root` 子树 → `<parent>/<target>`；元数据留在暂存目录，提交后删除。这样重命名后崩溃仍能从日志判定状态，且元数据不会污染最终目标。

## 不可变源计划（manifest）

枚举阶段（写任何文件之前）递归拉取 contents API（顺带支持 Link 头分页，>1000 条目目录不再漏项），每个文件一项：

```json
{"path": "相对路径", "url": "download_url", "git_sha": "<blob sha1>", "size": 123, "sha256": null}
```

- `manifest_id = sha256(canonical_json(manifest))`；`manifest.json` 落盘到暂存目录并 fsync。
- 枚举结果确定后即冻结；恢复时“请求的清单”由重新枚举得到（同一 git 引用 → 同一 blob 集合），与日志 BEGIN 记录的 `manifest_id` 比对，不同即陈旧/不匹配。

## 哈希链日志（tx.journal）

- JSON Lines，每行 `<base64(payload)>.<sha256(prev_digest + payload)>`；首条前驱为全零。
- 记录类型：`BEGIN`（format 版本、target 绝对路径、manifest_id、文件数、总字节、创建时间）、`FILE`（path、sha256、git_sha、size）、`PREPARED`（整树根 sha256）、`COMMITTED`、`CLEANED`。
- 恢复时重放并逐行校验链：撕裂的最后一行（写一半）丢弃；链断裂/校验和错误 → 判定损坏，仅允许报告，拒绝自动变更。
- 每次追加后 `flush + fsync`。

## 持久化顺序（写入与元数据）

1. `mkdir` 暂存目录 → fsync `<parent>`；写 `manifest.json` → fsync 文件 → fsync 暂存目录。
2. 追加 `BEGIN` → fsync 日志；建 `root/` → fsync 暂存目录。
3. 每个文件：流式下载到 `root/<path>`（先建父目录），同步计算 sha256 与 git blob sha1，与清单比对；不符立即中止（事务保留，等待恢复决策）；关闭句柄并 fsync 文件，追加 `FILE` 记录。
4. 全部文件后，目录自最深向最浅依次 fsync（保证目录项按持久化顺序落盘），最后 fsync 暂存目录。
5. 追加 `PREPARED` → fsync 日志。

## 原子发布与“重命名前后立即出错”恢复

发布序列（持锁）：

1. 再次 `stat` 确认最终目标不存在（防协作外进程抢先建目标）。
2. 同文件系统断言：`os.stat(staging).st_dev == os.stat(parent).st_dev`，不同则报错中止。
3. NOREPLACE 重命名 `root` → target：
   - Linux：ctypes 调 `renameat2(RENAME_NOREPLACE)`，不可用时回退“持锁 + 紧邻重检 + os.rename”；
   - macOS/BSD：持锁重检 + `os.rename`（其语义为目标存在时报错，不替换目录）；
   - Windows：`os.rename`（MoveFileExW 不带 REPLACE_EXISTING，目标存在即失败）；所有句柄事先关闭，对 `ERROR_ACCESS_DENIED/ERROR_SHARING_VIOLATION`（杀软/索引器占用）做有限次退避重试。
4. 重命名成功后 fsync `<parent>`，持久化目录项变化。
5. 追加 `COMMITTED` → fsync 日志；删除暂存目录 → fsync `<parent>` → 追加 `CLEANED`（尽力）。

崩溃窗口与恢复判定（确定性）：

| 崩溃点 | 现场 | 恢复动作 |
|---|---|---|
| 3 之前 | 暂存 + root 部分存在，日志无 PREPARED/有 PREPARED | 清单匹配：resume 时逐文件重校验，完好的跳过、缺失/损坏的重下，再走发布；cleanup 删暂存 |
| 3 报错后 | target 不在、root 在 | 等同未发布，可重试发布 |
| 3 与 5 之间 | target 在、root 在或不在、无 COMMITTED | 对最终树按 manifest 做完整校验：通过则补 COMMITTED 并清理（幂等成功）；不通过则只报告，**不触碰** target |
| 5 之后、清理中 | target 在、暂存有残留 | 重放日志见 COMMITTED → 校验 target 后重试清理 |

## 启动时中断检查与 CLI

新增参数：`--on-interrupted {resume,cleanup,report}`（默认 `report`）、`--no-transaction`（回退旧行为的逃生门）。

- 启动后、下载前：扫描 `output_dir` 及当前目录下的 `.gitdir-tx-*`；每个目标在预留前也做定向检查。
- `report`（默认）：只读重放日志，打印目标、manifest_id、文件完成数、状态（未准备/已准备/已提交/损坏/清单不匹配），损坏或不匹配时该目标不执行并以非零码退出。
- `resume`：仅当重新枚举得到的 manifest_id 与日志 BEGIN 一致时执行续传；不一致 → 明确报错（打印两个 id）并拒绝。
- `cleanup`：同样要求清单匹配才删除暂存；不匹配拒绝，提示人工处理。
- 目标已存在：打印“目标已存在，沿用既有覆盖策略（覆盖确认不在事务契约范围内）”，走旧流程。
- 另一个 gitdir 进程正在活动：`flock`/`msvcrt` 阻塞等锁（带超时），拿到后按日志状态决定跟随结果或恢复；进程死亡时内核自动释放锁，不会留下僵死互斥。

## 实施步骤（依赖序）

1. `transaction.py`：异常类型、暂存路径/命名、清单规范化与 id、目录/文件 fsync 辅助（POSIX 全量目录 fsync；Windows 跳过目录 fsync 并用 FILE_FLAG_BACKUP_SEMANTICS 尽力 FlushFileBuffers）。
2. 哈希链日志读写/校验（含撕裂尾行处理）、跨平台锁上下文管理器。
3. 事务主流程：reserve → 写清单/BEGIN → 下载校验落盘（FILE 记录）→ 目录 fsync → PREPARED。
4. 原子发布：st_dev 断言、NOREPLACE 三平台重命名、父目录 fsync、COMMITTED、清理。
5. 恢复器：日志重放、状态机（表中四窗口）、resume/cleanup/report 三动作与清单匹配闸门。
6. 改 `gitdir.py`：API 递归枚举（含分页）成 manifest；下载回调接引擎；CLI 接线与启动扫描；目标存在/flatten 回退旧路径；保留现有彩色输出风格。
7. `tests/test_transaction.py`：本地源端到端 + 各崩溃点注入（monkeypatch rename/fsync/删除）、日志撕裂与篡改、清单不匹配三动作闸门、两进程并发、st_dev 不匹配拒绝、恢复后文件字节一致。
8. 全量跑测试；如网络允许，对一个小型公开 GitHub 目录做一次真实冒烟（含 `report` 输出）。

## 依赖与注意事项

- 不新增第三方依赖；ctypes 用法全部做 `AttributeError/OSError` 回退。
- `renameat2` 在 glibc 过旧环境没有包装号时回退重检方案，并在日志/输出中标注。
- GitHub 匿名 API 限流（60 次/小时）属既有约束，不在本次范围。
- 目标路径定义对齐为 `<output_dir>/<download_dirs>`（默认 `-d ./` 时与现状输出位置一致；显式 `-d` 现在会被一致地加上前缀，顺带修正现有的 output_dir 拼接不一致）。
- Windows 上确保重命名前没有残留打开句柄（文件句柄用完即关；日志文件位于不被重命名的暂存目录内）。

## 验证

- `python3 -m unittest gitdir.tests.test_transaction -v` 全绿（POSIX 本机）。
- 手工注入：PREPARED 后 kill -9、重命名后杀进程、删掉半个 journal 行、篡改 manifest、用不同源清单请求 resume——行为逐一符合状态表。
- 并发：两个进程同一 URL/目标，只有一个执行发布，另一个跟随为成功，无覆盖。
- 真实冒烟（网络允许时）：下载→中断→resume→最终目录字节完整；再跑一次目标已存在→走旧路径并提示。

## 风险

- **协作外进程在重检后创建 target（POSIX 无原子 NOREPLACE 的平台）**：Linux 用 renameat2 消除窗口；macOS 接受持锁+紧邻重检的残余窗口，发生时后续校验会发现 target 非本次内容并只报告、不清理。
- **Windows 杀软占用导致重命名失败**：句柄全关 + 退避重试；仍失败则事务保持 PREPARED，下次 resume 重试，不留半成品在最终位置。
- **download_url 内容漂移**：以 git blob sha1 + sha256 双重校验，任何漂移都会中止事务而非落盘错误内容。
- **电源崩溃导致目录项未持久化**：严格按文件→目录（深到浅）→日志→重命名→父目录的 fsync 顺序；恢复以实际文件重校验为准，不信任仅凭日志声明。
