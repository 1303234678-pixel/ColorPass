## 改动目的

<!-- 这个 PR 解决什么问题？对应哪个 issue？ -->

## 改动范围

- [ ] 草稿解析（`draftio.py` / `plain.py` / `memscan.py`）
- [ ] 落轨（`send.py`）
- [ ] 回写（`pull.py` / `draftbuild.py` / `write.py`）
- [ ] 面板或 CLI
- [ ] 文档

## 验证方式

<!-- 有真实草稿实测就写实测结论（时长、段数、断言项）；没有就说明为什么只能在单测里覆盖 -->

- [ ] `python -m unittest discover -s tests -p "test_*.py"` 全绿
- [ ] `python -m ruff check colorpass tests --line-length 100` 无告警
- [ ] 新增/修改的用例在修复前会失败

## 影响面

<!-- 会不会改变写回行为、缓存指纹、面板交互？老版本产生的缓存/草稿是否需要迁移？ -->
