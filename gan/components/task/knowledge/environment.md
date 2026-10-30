# 环境事实（自检与运行前必读）

- 解释器：`python` 与 `python3` 都解析为 **venv 解释器**（与 harness 同一
  解释器栈，含项目依赖；`/usr/bin/python3` 为系统 Python，仍可用）；
  语法自检 `python -m py_compile <file>` 或 `python3 -m py_compile <file>`
  均可。
- bash 工具是**一次性 `bash -c`**：每次调用都是全新 shell，`cd`/export
  不跨调用保留——有依赖的命令用 `&&` 或 `;` 写在同一条命令里；非零退出码
  会以 `[exit N]` 附在结果尾部；命令超过 120s 会被整组终止并在错误文本里
  附带输出尾部。
- 沙箱无网络：`pip install` / `apt` / 任何联网命令都会失败，使用镜像内
  已安装的包。
