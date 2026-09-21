"""Evaluation dataset for Nexus Search Phase 4."""
import json
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

EVAL_DIR = Path(__file__).parent / "data"
EVAL_DIR.mkdir(exist_ok=True)


@dataclass
class Document:
    doc_id: str
    title: str
    content: str
    doc_type: str = "text"
    metadata: dict = None


@dataclass
class Query:
    query_id: str
    text: str
    relevant_docs: dict[str, int]  # doc_id -> relevance (0-3)


def create_benchmark_dataset() -> tuple[list[Document], list[Query]]:
    """Create a small deterministic benchmark dataset for evaluation."""
    documents = [
        Document("doc1", "Introduction to Machine Learning",
                 "Machine learning is a subset of artificial intelligence that enables systems to learn from data without explicit programming. It includes supervised learning, unsupervised learning, and reinforcement learning.",
                 "text"),
        Document("doc2", "Deep Learning Fundamentals",
                 "Deep learning uses neural networks with multiple layers to model complex patterns in data. Convolutional neural networks excel at image recognition, while recurrent networks handle sequential data.",
                 "text"),
        Document("doc3", "Natural Language Processing with Transformers",
                 "Transformers revolutionized NLP with attention mechanisms. Models like BERT and GPT use self-attention to understand context in text. They power modern chatbots, translation, and summarization.",
                 "text"),
        Document("doc4", "Vector Databases and Similarity Search",
                 "Vector databases store embeddings for efficient similarity search. They use indexing structures like HNSW or IVF to find nearest neighbors. Popular options include Pinecone, Weaviate, and Milvus.",
                 "text"),
        Document("doc5", "Information Retrieval and Search Engines",
                 "Search engines use inverted indexes and ranking algorithms like BM25. They tokenize documents, build postings lists, and score query-document pairs. Modern systems combine lexical and semantic search.",
                 "text"),
        Document("doc6", "Python for Data Science",
                 "Python is the primary language for data science. Libraries like pandas, numpy, scikit-learn, and matplotlib enable data manipulation, modeling, and visualization. Jupyter notebooks provide interactive development.",
                 "text"),
        Document("doc7", "SQL and Relational Databases",
                 "SQL is the standard language for relational databases. It supports queries, joins, aggregations, and transactions. Popular systems include PostgreSQL, MySQL, and SQLite for embedded use cases.",
                 "text"),
        Document("doc8", "Distributed Systems and Microservices",
                 "Distributed systems split workloads across multiple nodes. Microservices architecture decomposes applications into loosely coupled services. Communication uses REST, gRPC, or message queues.",
                 "text"),
        Document("doc9", "Cloud Computing and AWS",
                 "Cloud platforms provide on-demand compute, storage, and services. AWS offers EC2, S3, Lambda, and RDS. Serverless architectures reduce operational overhead with pay-per-use pricing.",
                 "text"),
        Document("doc10", "Kubernetes and Container Orchestration",
                 "Kubernetes automates container deployment, scaling, and management. It uses pods, services, and deployments. Helm charts package applications for reproducible deployments.",
                 "text"),
        Document("doc11", "Computer Vision and Image Processing",
                 "Computer vision enables machines to interpret visual data. Techniques include object detection, segmentation, and classification. OpenCV and PyTorch provide tools for image processing pipelines.",
                 "text"),
        Document("doc12", "Reinforcement Learning and Robotics",
                 "Reinforcement learning trains agents through rewards and penalties. Applications include robotics control, game playing, and autonomous systems. Algorithms include Q-learning, PPO, and DQN.",
                 "text"),
        Document("doc13", "Time Series Analysis and Forecasting",
                 "Time series analysis models temporal data for forecasting. ARIMA, Prophet, and LSTM networks handle trends and seasonality. Applications include demand planning and financial modeling.",
                 "text"),
        Document("doc14", "Graph Neural Networks",
                 "Graph neural networks operate on graph-structured data. They aggregate neighbor information through message passing. Applications include social networks, molecular modeling, and recommendation systems.",
                 "text"),
        Document("doc15", "Generative AI and Large Language Models",
                 "Generative AI creates new content including text, images, and code. Large language models like GPT-4 and Claude demonstrate emergent capabilities. Fine-tuning adapts models to specific domains.",
                 "text"),
        Document("doc16", "Data Engineering and ETL Pipelines",
                 "Data engineering builds pipelines for collecting, transforming, and loading data. Tools include Airflow, dbt, and Spark. Data quality, lineage, and governance are key concerns.",
                 "text"),
        Document("doc17", "Recommendation Systems",
                 "Recommendation systems suggest relevant items to users. Collaborative filtering uses user-item interactions. Content-based methods use item features. Hybrid approaches combine both signals.",
                 "text"),
        Document("doc18", "Anomaly Detection and Fraud Prevention",
                 "Anomaly detection identifies unusual patterns in data. Statistical methods, isolation forests, and autoencoders detect outliers. Applications include fraud detection, network security, and quality control.",
                 "text"),
        Document("doc19", "Feature Engineering and Selection",
                 "Feature engineering transforms raw data into predictive features. Techniques include encoding, scaling, polynomial features, and domain-specific transformations. Selection methods reduce dimensionality.",
                 "text"),
        Document("doc20", "Model Evaluation and ML Metrics",
                 "Model evaluation measures predictive performance. Classification metrics include accuracy, precision, recall, F1, and AUC. Regression uses MSE, MAE, and R-squared. Cross-validation ensures robust estimates.",
                 "text"),
        Document("doc21", "MLOps and Model Deployment",
                 "MLOps applies DevOps principles to machine learning. CI/CD pipelines automate training, testing, and deployment. Monitoring detects drift and performance degradation. Tools include MLflow and Kubeflow.",
                 "text"),
        Document("doc22", "Ethics and Bias in AI",
                 "AI ethics addresses fairness, transparency, and accountability. Bias can emerge from training data, algorithm design, or deployment context. Responsible AI practices include auditing and diverse development teams.",
                 "text"),
        Document("doc23", "Edge Computing and IoT",
                 "Edge computing processes data near its source. IoT devices generate massive data streams. Lightweight models run on constrained hardware. Federated learning enables privacy-preserving training.",
                 "text"),
        Document("doc24", "Quantum Computing and Machine Learning",
                 "Quantum computing uses qubits for parallel computation. Quantum machine learning explores speedups for optimization and linear algebra. NISQ devices enable near-term experimentation.",
                 "text"),
        Document("doc25", "AI Safety and Alignment",
                 "AI safety ensures systems behave as intended. Alignment research addresses reward specification, interpretability, and robustness. Techniques include constitutional AI and debate.",
                 "text"),
    ]

    queries = [
        Query("q1", "machine learning basics", {"doc1": 3, "doc6": 1, "doc20": 1}),
        Query("q2", "neural networks deep learning", {"doc2": 3, "doc3": 2, "doc15": 1}),
        Query("q3", "transformer attention NLP", {"doc3": 3, "doc15": 2, "doc11": 1}),
        Query("q4", "vector database similarity search", {"doc4": 3, "doc5": 2, "doc17": 1}),
        Query("q5", "search engine ranking BM25", {"doc5": 3, "doc4": 1, "doc17": 1}),
        Query("q6", "python data science pandas", {"doc6": 3, "doc16": 1, "doc19": 1}),
        Query("q7", "SQL database queries", {"doc7": 3, "doc16": 1}),
        Query("q8", "microservices distributed systems", {"doc8": 3, "doc10": 1, "doc21": 1}),
        Query("q9", "cloud AWS serverless", {"doc9": 3, "doc10": 1, "doc23": 1}),
        Query("q10", "Kubernetes containers orchestration", {"doc10": 3, "doc9": 1, "doc23": 1}),
        Query("q11", "computer vision image recognition", {"doc11": 3, "doc2": 2, "doc15": 1}),
        Query("q12", "reinforcement learning robotics", {"doc12": 3, "doc2": 1, "doc23": 1}),
        Query("q13", "time series forecasting ARIMA", {"doc13": 3, "doc19": 1, "doc20": 1}),
        Query("q14", "graph neural networks", {"doc14": 3, "doc8": 1, "doc17": 1}),
        Query("q15", "generative AI large language models", {"doc15": 3, "doc3": 2, "doc22": 1}),
        Query("q16", "data engineering ETL pipelines", {"doc16": 3, "doc6": 1, "doc21": 1}),
        Query("q17", "recommendation system collaborative filtering", {"doc17": 3, "doc5": 1, "doc14": 1}),
        Query("q18", "anomaly detection fraud", {"doc18": 3, "doc20": 1, "doc21": 1}),
        Query("q19", "feature engineering selection", {"doc19": 3, "doc6": 1, "doc20": 1}),
        Query("q20", "model evaluation metrics precision recall", {"doc20": 3, "doc1": 1, "doc17": 1}),
        Query("q21", "MLOps deployment monitoring", {"doc21": 3, "doc9": 1, "doc10": 1}),
        Query("q22", "AI ethics bias fairness", {"doc22": 3, "doc15": 1, "doc25": 1}),
        Query("q23", "edge computing IoT federated learning", {"doc23": 3, "doc12": 1, "doc25": 1}),
        Query("q24", "quantum computing machine learning", {"doc24": 3, "doc2": 1, "doc14": 1}),
        Query("q25", "AI safety alignment", {"doc25": 3, "doc15": 2, "doc22": 1}),
    ]

    return documents, queries


def save_dataset(documents: list[Document], queries: list[Query], path: str = None):
    if path is None:
        path = EVAL_DIR / "benchmark_dataset.json"
    data = {
        "documents": [asdict(d) for d in documents],
        "queries": [asdict(q) for q in queries],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_dataset(path: str = None) -> tuple[list[Document], list[Query]]:
    if path is None:
        path = EVAL_DIR / "benchmark_dataset.json"
    with open(path) as f:
        data = json.load(f)
    documents = [Document(**d) for d in data["documents"]]
    queries = [Query(**q) for q in data["queries"]]
    return documents, queries


if __name__ == "__main__":
    docs, queries = create_benchmark_dataset()
    save_dataset(docs, queries)
    print(f"Created dataset with {len(docs)} documents and {len(queries)} queries")