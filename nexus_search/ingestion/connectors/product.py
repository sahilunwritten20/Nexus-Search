"""Product connector: parses CSV/JSON product catalogs into indexable documents."""
import csv
import json
from pathlib import Path
from typing import Iterator

from ..types import IngestDoc


def _product_to_doc(product: dict, source: str, index: int) -> IngestDoc:
    raw_id = product.get("id") or product.get("sku") or f"{source}-{index}"
    doc_id = str(raw_id)
    title = str(product.get("name") or product.get("title") or doc_id)

    # Index every scalar field's value so name, description, category etc.
    # are all searchable together, not just whichever field we called "title".
    parts = [str(v) for v in product.values() if isinstance(v, (str, int, float))]
    content = " ".join(parts)

    metadata = {k: v for k, v in product.items() if isinstance(v, (str, int, float, bool))}

    return IngestDoc(doc_id=f"product:{doc_id}", title=title, content=content,
                      doc_type="product", metadata=metadata)


def iter_products_csv(path: str) -> Iterator[IngestDoc]:
    p = Path(path)
    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            yield _product_to_doc(dict(row), source=p.stem, index=i)


def iter_products_json(path: str) -> Iterator[IngestDoc]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else data.get("products", [])
    for i, record in enumerate(records):
        yield _product_to_doc(record, source=p.stem, index=i)


def iter_products(path: str) -> Iterator[IngestDoc]:
    p = Path(path)
    if p.suffix.lower() == ".csv":
        yield from iter_products_csv(path)
    elif p.suffix.lower() == ".json":
        yield from iter_products_json(path)
    else:
        raise ValueError(f"Unsupported product file type: {p.suffix} (use .csv or .json)")
