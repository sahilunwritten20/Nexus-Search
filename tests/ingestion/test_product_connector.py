import shutil
import tempfile
import unittest
from pathlib import Path

from nexus_search.ingestion.connectors.product import iter_products, iter_products_csv, iter_products_json


class TestProductConnectorCsv(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.csv_path = Path(self.root, "products.csv")
        self.csv_path.write_text(
            "id,name,description,price\n"
            "1,Blue Mug,A ceramic mug in blue,12.99\n"
            "2,Red Mug,A ceramic mug in red,12.99\n"
        )

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_parses_rows_into_docs(self):
        docs = list(iter_products_csv(str(self.csv_path)))
        self.assertEqual(len(docs), 2)

    def test_uses_id_field_for_doc_id(self):
        docs = {d.doc_id: d for d in iter_products_csv(str(self.csv_path))}
        self.assertIn("product:1", docs)
        self.assertIn("product:2", docs)

    def test_title_from_name_field(self):
        docs = {d.doc_id: d for d in iter_products_csv(str(self.csv_path))}
        self.assertEqual(docs["product:1"].title, "Blue Mug")

    def test_content_includes_description(self):
        docs = {d.doc_id: d for d in iter_products_csv(str(self.csv_path))}
        self.assertIn("ceramic mug in blue", docs["product:1"].content)

    def test_doc_type_is_product(self):
        docs = list(iter_products_csv(str(self.csv_path)))
        self.assertTrue(all(d.doc_type == "product" for d in docs))


class TestProductConnectorJson(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_parses_list_json(self):
        path = Path(self.root, "products.json")
        path.write_text('[{"id": "1", "name": "Widget", "description": "A useful widget"}]')
        docs = list(iter_products_json(str(path)))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].title, "Widget")

    def test_parses_wrapped_json(self):
        path = Path(self.root, "products.json")
        path.write_text('{"products": [{"id": "1", "name": "Widget"}]}')
        docs = list(iter_products_json(str(path)))
        self.assertEqual(len(docs), 1)

    def test_missing_id_falls_back_to_generated_id(self):
        path = Path(self.root, "products.json")
        path.write_text('[{"name": "No ID Product"}]')
        docs = list(iter_products_json(str(path)))
        self.assertTrue(docs[0].doc_id.startswith("product:"))


class TestIterProductsDispatch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_dispatches_csv(self):
        path = Path(self.root, "p.csv")
        path.write_text("id,name\n1,Thing\n")
        docs = list(iter_products(str(path)))
        self.assertEqual(len(docs), 1)

    def test_dispatches_json(self):
        path = Path(self.root, "p.json")
        path.write_text('[{"id": "1", "name": "Thing"}]')
        docs = list(iter_products(str(path)))
        self.assertEqual(len(docs), 1)

    def test_unsupported_extension_raises(self):
        path = Path(self.root, "p.xml")
        path.write_text("<products></products>")
        with self.assertRaises(ValueError):
            list(iter_products(str(path)))


if __name__ == "__main__":
    unittest.main()
