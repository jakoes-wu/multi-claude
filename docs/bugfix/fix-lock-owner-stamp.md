# 令牌刷新锁的归属指纹在 ext4 上失效

> 2026-10-16 注记：已落地。`src/multi_claude/refresh.py:248`（`_set_mtime_now`）、`:242`（建锁时设时间，失败降级）、`:268`（保活改用它）；类说明在 `:229`。新用例 `tests/test_refresh.py:488`（`LockStampTest`），放到 main 源码上会失败。验证：编译机（Ubuntu 20、ext4、Python 3.8.10）R1 原用例 50 / 50 通过；R4 基线 main `2f3ba45` 同一用例 20 次里 5 次 `FAILED (failures=1)`；R3 全量 498 个通过、7 个按环境跳过；R2 通过。本机 macOS（Python 3.11）全量 498 个通过、4 个跳过。设计评审 2 轮、代码评审 1 轮收敛。

## 1. 背景

`multi-claude list` 刷新过期令牌时，用 `mkdir` 建两个锁目录（与 Claude Code 的 proper-lockfile 同一套锁），持锁期间每 5 秒更新一次锁目录的修改时间，结束时 `rmdir` 放锁。放锁和保活之前，都先核对这个路径上的目录还是不是自己建的那个：进程被挂起超过 60 秒时，锁可能已被别人当作过期锁删掉重建，那就不能再碰它。

核对用的指纹是 `(设备号, inode, 修改时间纳秒)`（`src/multi_claude/refresh.py:240-245`）。用例 `test_f7_lock_taken_over_by_someone_else_is_left_alone`（`tests/test_refresh.py:257-269`）在持锁期间把两把锁原地删掉重建，期望放锁后两把锁都还在。这个用例在编译机（Ubuntu 20、ext4、Python 3.8.10）上单独重跑有约 2–6 成失败，本机 macOS 从未失败。

## 2. 目标 / 非目标

目标：

1. 在 ext4 上，锁被删掉、立即在同一路径重建后，`_Lock.is_mine()` 返回假，放锁和保活都不碰别人的锁。
2. 上述用例在编译机上连续 50 次通过。

非目标：

- 不改锁的路径、加锁顺序、过期判定（300 秒）和保活间隔。
- 不处理修改时间精度低于微秒的文件系统（见 §3）。
- 不处理 `touch()` 里“核对归属”和 `os.utime` 之间那一瞬间锁被换掉的竞态（现有行为，见 §7 第 4 条）。

## 3. 假设与约束

- 锁目录建在账号配置目录里（`refresh.py:224-226`），一般在 home 下，文件系统是 ext4 或 APFS，修改时间都存到纳秒。exFAT（10 毫秒）、FAT（2 秒）和部分 NFS 会截掉低位，在这些文件系统上本方案的区分能力会下降，仍可能撞上。
- Claude Code 的 proper-lockfile 判断锁是否过期，只看锁目录的修改时间离现在是否超过 60 秒（`docs/feature/feature-token-refresh.md` §3 第 5、6 条）。本方案把修改时间设为“现在”，不改变这个判断。
- 不能往锁目录里放文件：proper-lockfile 用 `rmdir` 清理过期锁，目录里有文件会删不掉，锁会一直卡住。
- 需要 Python 3.8 起就有的 `time.time_ns()` 与 `os.utime(path, ns=...)`；项目最低支持 3.8。

## 4. 涉及模块

| 区域 | 行号锚点（main `2f3ba45`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/refresh.py` `_Lock` 类说明 | 229-235 | 修改 | 说明指纹里的修改时间由本工具用精细时钟设置，以及原因 |
| `src/multi_claude/refresh.py` `_Lock.__init__` | 236-238 | 修改 | 先设修改时间再记指纹 |
| `src/multi_claude/refresh.py` 新增 `_Lock._set_mtime_now` | 238 之后追加 | 新增 | 用 `time.time_ns()` 设锁目录的访问和修改时间 |
| `src/multi_claude/refresh.py` `_Lock.touch` | 255 | 修改 | `os.utime(self.path, None)` 改为调用 `_set_mtime_now()` |
| `tests/test_refresh.py` 新增 `LockStampTest` | 483 之后（`KeychainWriteTest` 之前）追加 | 新增 | 建锁和保活后，修改时间等于桩出来的 `time_ns` 值 |
| `docs/feature/feature-token-refresh.md` | 117、131 | 修改 | 归属核对的描述从“设备号与 inode”改为三项指纹，并说明修改时间的来源 |
| `CHANGELOG.md` `[Unreleased]` | 7 之后 | 新增 | Fixed 一条 |

## 5. 根因分析

- **现象**：上述用例在编译机上偶发失败，放锁后锁目录不见了。
- **触发条件**：锁目录被删后立即在同一路径重建，前后间隔小于一个内核时钟节拍（几毫秒），而且文件系统复用了 inode 编号。
- **失效路径**：
  1. `_make_lock` 里 `os.mkdir` 成功后，`_Lock.__init__` 读到的修改时间是内核写的（`refresh.py:238`、`:245`）。实测在 ext4 上，几毫秒内先后建的两个目录修改时间完全相同（见下表 1993 / 2000），说明内核记的时间按时钟节拍跳变、分辨不出这么短的间隔（内核实现未在仓库内核实）。
  2. 用例在持锁期间 `os.rmdir` 加 `os.mkdir`（`tests/test_refresh.py:261-265`）。ext4 把刚释放的 inode 编号立刻分给新目录，新目录的修改时间又落在同一个节拍里。
  3. 放锁时 `is_mine()` 读到的三项与指纹完全相同（`refresh.py:247-248`），`remove()` 把别人的锁 `rmdir` 掉了（`refresh.py:264-271`）。
- **错误代码点**：`refresh.py:238` 的指纹完全取自内核给的修改时间，这个时间分辨不出“同一节拍里先后建的两个目录”；`refresh.py:255` 保活时 `os.utime(self.path, None)` 同样让内核取时间，有同样问题。
- **最小复现证据**（2026-10-16，脚本 `lock_repro.py`：建目录、读三项、删掉、重建、再读三项，各 2000 次）：

| 环境 | inode 复用 | 三项全同 |
| ---- | ---- | ---- |
| 编译机 ext4，内核记的修改时间（现状） | 2000 / 2000 | 1993 / 2000 |
| 编译机 ext4，建目录后用 `time.time_ns()` 设修改时间（本方案） | 1998 / 2000 | 0 / 2000 |
| 本机 macOS APFS，两种都 | 0 / 2000 | 0 / 2000 |

真实运行里，接管只会发生在锁闲置 300 秒（本工具）或 60 秒（proper-lockfile）之后，删和建之间也隔着别的进程的判断，同一节拍里完成的可能性很小。所以主要影响是用例不稳定；对用户的影响面很小，但不为零。

## 6. 复现步骤

1. 在 ext4 上的 Linux 机器（如编译机，Ubuntu 20、Python 3.8.10）取 main `2f3ba45` 的源码。
2. 进入源码的 `tests/` 目录运行（测试靠 `from helpers import ...` 导入辅助模块，必须在 `tests/` 里跑）：`for i in $(seq 20); do python3 -m unittest test_refresh.RenewTest.test_f7_lock_taken_over_by_someone_else_is_left_alone 2>&1 | tail -1; done`。
3. 20 次里会出现若干次 `FAILED (failures=1)`，断言是 `[False, False] != [True, True]` 或其中一项为 `False`。只认 `Ran 1 test` 之后的 `OK` 与 `FAILED (failures=1)`；出现 `errors=` 是导入或环境错误，不算复现。

## 7. 影响版本

v0.9.1 起（令牌刷新随 `6064ab3` 引入，`git tag --contains 6064ab3 --sort=version:refname` 第一个是 v0.9.1）至 v0.12.0，v0.12.1 修复。只在会复用 inode、且修改时间由粗时钟写入的文件系统上触发：实测 ext4 会；APFS 不复用 inode，不触发。

## 8. 方案

### 8.1 实现要点

1. 在 `_Lock` 里新增 `_set_mtime_now()`：取 `t = time.time_ns()`，`os.utime(self.path, ns=(t, t))`。用户态时钟读的是此刻的时间，而内核给新目录的时间停在上一个节拍，别人重建的目录拿到的是节拍时间，与它几乎不可能相同。
2. `__init__`（`refresh.py:236-238`）：先调用 `_set_mtime_now()`，再 `self._stamp = self._read()`。`os.utime` 抛 `OSError` 时不报错，直接用内核写的时间记指纹，等于现状：这时锁照样能用，只是退回现有的区分能力。不能因为设时间失败就把已经建好的锁当成取锁失败，否则要另外处理放掉这把锁的路径，超出本方案。
3. `touch()`（`refresh.py:255`）：`os.utime(self.path, None)` 改为 `self._set_mtime_now()`，保留外层的 `try/except OSError: return`。不改的话，保活一次之后指纹又退回节拍时间，漏洞重新出现。保活后“设备号与 inode 没变才采纳新时间”的检查（`refresh.py:259-262`）不变。
4. 在 `_Lock` 类说明里写清：修改时间由本工具用精细时钟设置，因为 ext4 删掉重建会复用 inode、内核记的时间又按节拍走，只靠内核的值分辨不出别人重建的锁；同时注明不能改成往锁目录里放标记文件（proper-lockfile 用 `rmdir` 清理过期锁）。
5. 新增用例 `LockStampTest`：
   - 用普通 `unittest.TestCase` 加临时目录，先 `os.mkdir` 建锁目录再构造 `_Lock`（`_Lock` 本身不建目录），不起假令牌服务、不跑 CLI。`refresh.time` 就是标准库 `time` 模块，打桩会影响整个进程，所以 `mock.patch.object(refresh.time, "time_ns", return_value=T)` 只包住 `_Lock(...)` 和 `touch()` 这两次调用。
   - 用该桩，`T` 取一个低位非零的值（如 `1_700_000_000_123_456_789`），建一个 `_Lock`，断言 `os.lstat(path).st_mtime_ns == T` 且 `is_mine()` 为真。
   - 再把桩改成 `T + 5_000_000_000`，调用 `touch()`，断言修改时间等于新值、`is_mine()` 仍为真。
   - 该用例不依赖 inode 复用，本机和编译机上都确定性地验证“修改时间来自 `time_ns`”这一机制；ext4 上的端到端效果由原用例连跑 50 次验证。

### 8.2 接口变更

无接口变更。锁的路径、目录形态、过期判定和对外的刷新结果（`lock_busy`、`lock_error` 等）都不变，`_Lock` 是模块私有类。

## 9. 备选方案与决策

| 备选 | 结论 |
| ---- | ---- |
| 在锁目录里放一个随机标记文件作归属凭据 | 否决：proper-lockfile 用 `rmdir` 清理过期锁，目录非空会失败，本工具被强杀后留下的锁会一直卡住 Claude Code 的刷新 |
| 指纹加上 `st_ctime_ns` | 否决：ctime 同样由内核粗时钟写入，不增加区分能力 |
| 把修改时间设成“现在”加一个随机纳秒偏移 | 不采用：`time_ns()` 已足以与节拍时间区分（复现 0 / 2000）；随机偏移在截掉低位的文件系统上同样无效，只增加复杂度 |

## 10. 影响分析

1. **本工具的锁判断**：`is_mine()` 的两个调用方是 `touch()`、`remove()`（`refresh.py:252`、`:266`），都只拿它和自己的指纹比较，指纹来源换了不影响比较逻辑。`_remove_if_dead`（`refresh.py:302-315`）用 `st_mtime` 判断别人的残留锁是否超过 300 秒，读的是别人的锁，不受影响。
2. **与 Claude Code 并用**：proper-lockfile 只用修改时间离现在多久判断过期。本方案设的时间就是“现在”，比内核节拍时间最多晚几毫秒，60 秒的判断不受影响。现状下本工具的保活本来就会改写锁的修改时间，本方案只是改了取时间的来源，没有新增 Claude Code 能观察到的行为。
3. **运行时开销**：建锁时每把锁多一次 `utime` 系统调用（每次刷新两把）；保活本来就有一次 `utime`，只是换了参数。不新增线程、锁或阻塞操作。
4. **未变的竞态**：`touch()` 在 `is_mine()` 与 `utime` 之间锁若被换掉，会把别人锁的时间改掉；之后的“设备号与 inode 没变才采纳”检查在 inode 复用时挡不住。这个窗口只有几微秒，而且要求锁已闲置 60 秒以上，现状就有，本方案不扩大也不处理。
5. **文件系统差异**：APFS 上行为不变（本来就不复用 inode）；修改时间精度低的文件系统上退化为现状（见 §3）。

## 11. 回归测试

| 编号 | 对应影响 | 方法 | 判据 |
| ---- | ---- | ---- | ---- |
| R1 | 目标 1、2 | 编译机源码快照的 `tests/` 目录里，按 §6 的命令单独运行原用例 50 次 | 50 次都是 `Ran 1 test` + `OK`；出现 `errors=` 判为无效重跑 |
| R2 | 8.1 第 5 条 | 本机与编译机运行 `LockStampTest` | 通过 |
| R3 | §10 第 1、2 条 | 本机与编译机全量 `python3 -m unittest discover -s tests` | 全部通过（按环境跳过的除外），含 `test_f7_busy_lock_is_respected`、`test_f7_dead_lock_is_cleared_and_a_live_one_is_not`、`test_lock_cannot_be_created` |
| R4 | 反向对照（证明 R1 的通过来自本方案） | 编译机上用 main `2f3ba45` 的快照，按 §6 的命令连跑原用例 20 次 | 至少出现一次 `FAILED (failures=1)`；`errors=` 不计 |

## 12. 日志 / 观测点

本工具的锁不打日志，本方案也不新增日志。观测点是：

- 用例输出：原用例与 `LockStampTest` 的 `OK` / `FAILED`；
- 现场排查时：`stat -c '%i %y' <配置目录>/.oauth_refresh.lock` 读锁目录的 inode 与修改时间；本工具持锁期间每 5 秒 `stat` 一次，修改时间应持续推进，inode 不变；锁被别人重建后，修改时间回到对方建目录时的值，之后按对方的节奏推进。
