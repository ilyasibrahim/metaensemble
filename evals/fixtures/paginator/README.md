# paginator

A tiny pagination helper. `paginate(items, page, page_size)` returns the
zero-indexed `page`-th chunk of a sequence, and `page_count(total, page_size)`
reports how many pages a collection needs. The module docstring in
`pagination.py` states the intended slicing contract.

Run the tests with:

```bash
pytest
```
