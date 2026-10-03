"""WP10 fixture: deterministic corpus + labeled queries (labels are ground
truth BY CONSTRUCTION; the protocol is the docstring + labels.py checks).

Topics each have a distinctive vocabulary; docs mix topic terms with
distractor text. Queries are built FROM the topic definitions so relevance
is known exactly: a query's relevant docs are the topic docs it was built
from, graded by construction (core topic doc = 3, secondary = 1).
"""
import random
from dataclasses import dataclass, field

SPLIT_SEED = 20260101

TOPICS = {
    "retrieval": {
        "core": ("BM25 and Lexical Retrieval",
                 "BM25 scores lexical matches over an inverted index. "
                 "Term frequency, inverse document frequency and length "
                 "normalization drive the ranking function."),
        "secondary": ("Query Parsing and Tokenization",
                      "The query parser splits phrases, fields and boolean "
                      "operators; the tokenizer folds case and stems words "
                      "before the index is consulted."),
        "vocab": ["bm25", "lexical", "inverted", "index", "ranking",
                  "tokenization", "parser", "query"],
    },
    "vector": {
        "core": ("Vector Embeddings and Similarity",
                 "Dense embeddings map text into a vector space where "
                 "cosine similarity approximates semantic relatedness. "
                 "Approximate nearest neighbor indexes speed the search."),
        "secondary": ("Embedding Models and Dimensions",
                      "Sentence embedding models compress meaning into a "
                      "fixed number of dimensions; the dimension trades "
                      "quality against memory."),
        "vocab": ["embedding", "vector", "cosine", "similarity", "dense",
                  "semantic", "dimension", "nearest"],
    },
    "crawler": {
        "core": ("Web Crawlers and Politeness",
                 "A crawler follows links from a frontier queue, honors "
                 "robots.txt and spaces requests per domain with a crawl "
                 "delay so hosts are never flooded."),
        "secondary": ("Sitemaps and URL Normalization",
                      "Sitemap files list crawlable URLs; normalization "
                      "drops fragments and tracking parameters so one page "
                      "has one canonical address."),
        "vocab": ["crawler", "robots", "politeness", "frontier", "sitemap",
                  "url", "canonical", "delay"],
    },
    "databases": {
        "core": ("Relational Databases and SQL",
                 "Relational databases store rows in tables; SQL expresses "
                 "joins, aggregations and transactions over them."),
        "secondary": ("Transactions and Isolation Levels",
                      "A transaction is all-or-nothing; isolation levels "
                      "trade consistency against concurrency for readers "
                      "and writers."),
        "vocab": ["sql", "database", "table", "join", "transaction",
                  "isolation", "row", "query"],
    },
    "auth": {
        "core": ("API Keys and Authentication",
                 "An API key authenticates a caller; constant-time "
                 "comparison prevents timing attacks that leak the secret."),
        "secondary": ("Authorization and Access Control",
                      "Authentication says who you are; authorization says "
                      "what you may do. Rate limiting bounds abuse either "
                      "way."),
        "vocab": ["auth", "key", "secret", "token", "permission", "access",
                  "rate", "limit"],
    },
    "graphs": {
        "core": ("PageRank and Link Graphs",
                 "A link graph records which pages vouch for which; "
                 "iterative PageRank distributes authority through the "
                 "edges until the scores converge."),
        "secondary": ("Connected Components and Traversal",
                      "Graph traversal visits nodes breadth-first; "
                      "connected components group the pages that can reach "
                      "each other."),
        "vocab": ["pagerank", "graph", "link", "authority", "edge",
                  "component", "traversal", "node"],
    },
    "ranking": {
        "core": ("Learning to Rank and Fusion",
                 "Learning to rank blends retrieval signals with learned "
                 "weights; score fusion merges keyword and semantic "
                 "candidate lists into one ordering."),
        "secondary": ("Feature Engineering for Rankers",
                      "A ranker consumes features: match signals, quality "
                      "scores, freshness decay. Feature normalization keeps "
                      "the scales comparable."),
        "vocab": ["rank", "fusion", "feature", "weight", "signal", "blend",
                  "learning", "ordering"],
    },
    "nlp": {
        "core": ("Language Models and Text",
                 "Language models predict the next token; text "
                 "classification and entity extraction build on the same "
                 "representations."),
        "secondary": ("Spell Correction and Normalization",
                      "Spell correction offers in-vocabulary candidates for "
                      "typos; normalization strips punctuation noise from "
                      "queries."),
        "vocab": ["language", "model", "text", "spell", "typo", "entity",
                  "classification", "token"],
    },
    "infra": {
        "core": ("Containers and Orchestration",
                 "Containers package an application with its runtime; an "
                 "orchestrator schedules them across a cluster with health "
                 "checks and restarts."),
        "secondary": ("Deployment and Health Probes",
                      "A readiness probe gates traffic to a container; a "
                      "liveness probe restarts one that stopped answering. "
                      "Deployments roll out gradually."),
        "vocab": ["container", "orchestration", "deploy", "health", "probe",
                  "cluster", "rollout", "restart"],
    },
    "testing": {
        "core": ("Automated Testing and Coverage",
                 "Automated tests pin behavior: unit tests cover functions, "
                 "integration tests cover seams, regression tests keep bugs "
                 "fixed."),
        "secondary": ("Test Doubles and Mocks",
                      "A test double stands in for a dependency; a mock "
                      "asserts interactions. Deterministic fixtures keep "
                      "suites stable."),
        "vocab": ["test", "coverage", "unit", "integration", "regression",
                  "mock", "fixture", "assert"],
    },
    "search": {
        "core": ("Search Engines End to End",
                 "A search engine ingests documents, builds an inverted "
                 "index, ranks candidates and serves pages of results with "
                 "snippets and facets."),
        "secondary": ("Snippets, Facets and Pagination",
                      "Result pages carry query-focused snippets, facet "
                      "counts for narrowing, and stable pagination windows "
                      "over the ranked pool."),
        "vocab": ["search", "engine", "snippet", "facet", "pagination",
                  "result", "page", "corpus"],
    },
    "security": {
        "core": ("SSRF and Input Validation",
                 "Server-side request forgery tricks a server into calling "
                 "itself; validating every resolved address and pinning DNS "
                 "closes the hole."),
        "secondary": ("Rate Limiting and Abuse",
                      "A rate limiter keys requests per client and returns "
                      "429 with a retry hint; abuse controls keep the "
                      "service available."),
        "vocab": ["ssrf", "validation", "input", "rate", "limit", "abuse",
                  "security", "request"],
    },
}

# paraphrase queries: reworded so keyword overlap with the target docs is
# near zero but the MEANING matches the core topic document
PARAPHRASES = {
    "retrieval": "how do engines score documents against user questions",
    "vector": "finding texts that mean the same thing using math",
    "crawler": "software that politely reads every page of a website",
    "databases": "asking structured questions of stored business records",
    "auth": "checking who is allowed to call the service",
    "graphs": "measuring which pages the rest of the web trusts most",
    "ranking": "combining several quality signals into one result order",
    "nlp": "programs that understand and fix written words",
    "infra": "running many copies of an app across many machines",
    "testing": "proof that the code still behaves after changes",
    "search": "a system that finds answers inside a pile of documents",
    "security": "stopping a server from being tricked into internal calls",
}

# synonym queries: synonyms of topic vocabulary
SYNONYMS = {
    "retrieval": ["lookup", "matching", "scoring"],
    "vector": ["embedding", "dense", "similar"],
    "crawler": ["spider", "fetcher", "politeness"],
    "databases": ["sql", "records", "tables"],
    "auth": ["credentials", "permission", "token"],
    "graphs": ["pagerank", "links", "edges"],
    "ranking": ["ordering", "weights", "fusion"],
    "nlp": ["language", "spelling", "words"],
    "infra": ["deployment", "clusters", "probes"],
    "testing": ["assertions", "coverage", "mocks"],
    "search": ["results", "snippets", "facets"],
    "security": ["validation", "forgery", "limits"],
}

# no-answer queries: real English, no topic anywhere in the corpus
NO_ANSWER = [
    "best recipe for sourdough bread at home",
    "how to train a puppy to stop barking",
    "population of the largest cities in south america",
    "who won the football world cup in 1998",
    "weather patterns in the pacific ocean this winter",
]


@dataclass
class FixtureDoc:
    doc_id: str
    title: str
    content: str
    topic: str


@dataclass
class FixtureQuery:
    query_id: str
    text: str
    category: str          # paraphrase | synonym | exact | multi | typo | no-answer
    relevant: dict = field(default_factory=dict)   # doc_id -> relevance 0-3


def _typo(word: str, rng: random.Random) -> str:
    """1-2 edit typo of `word` (transpose or drop a char), deterministic
    per rng state."""
    if len(word) < 4:
        return word
    kind = rng.random()
    i = rng.randrange(1, len(word) - 1)
    if kind < 0.5:  # transpose
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    return word[:i] + word[i + 1:]  # delete


def build_fixture() -> tuple[list[FixtureDoc], list[FixtureQuery]]:
    """Deterministic corpus + queries; labels are construction ground truth.
    Split decision happens in the runner via SPLIT_SEED (committed)."""
    rng = random.Random(SPLIT_SEED)
    docs: list[FixtureDoc] = []
    queries: list[FixtureQuery] = []

    for t_i, (topic, spec) in enumerate(sorted(TOPICS.items())):
        core_title, core_text = spec["core"]
        sec_title, sec_text = spec["secondary"]
        vocab = spec["vocab"]
        # 14 docs per topic: 1 core, 1 secondary, 12 variants with distractors
        for v in range(14):
            if v == 0:
                title, content = core_title, core_text
            elif v == 1:
                title, content = sec_title, sec_text
            else:
                # variants: topic terms + heavy distractor filler — keyword
                # matches exist but the CORE doc is the strong one
                filler = " ".join(
                    f"{vocab[(v + k) % len(vocab)]} note {v}{k}" for k in range(4))
                distractor = (f"generic background paragraph {v} about "
                              f"everyday topics with filler sentences "
                              f"number {v * 7 + k}" for k in range(3))
                title = f"{vocab[v % len(vocab)].title()} Notes {v}"
                content = filler + " " + " ".join(distractor)
            docs.append(FixtureDoc(f"{topic}-{v:02d}", title, content, topic))

        # queries per topic (6 total -> 72 across 12 topics):
        # exact: two vocab terms
        queries.append(FixtureQuery(
            f"{topic}-exact", f"{vocab[0]} {vocab[1]}", "exact",
            {f"{topic}-00": 3, f"{topic}-01": 1}))
        # exact: identifier-ish (core title words)
        queries.append(FixtureQuery(
            f"{topic}-ident", core_title.lower(), "exact",
            {f"{topic}-00": 3}))
        # synonym: three synonyms
        queries.append(FixtureQuery(
            f"{topic}-syn", " ".join(SYNONYMS[topic]), "synonym",
            {f"{topic}-00": 3, f"{topic}-01": 2}))
        # paraphrase
        queries.append(FixtureQuery(
            f"{topic}-para", PARAPHRASES[topic], "paraphrase",
            {f"{topic}-00": 3, f"{topic}-01": 1}))
        # multi-intent: this topic + the next topic's vocab
        other = sorted(TOPICS)[(t_i + 1) % len(TOPICS)]
        queries.append(FixtureQuery(
            f"{topic}-multi", f"{vocab[2]} {TOPICS[other]['vocab'][0]}", "multi",
            {f"{topic}-00": 2, f"{other}-00": 2}))
        # typo: two mistyped vocab terms
        typos = [_typo(vocab[3], rng), _typo(vocab[4], rng)]
        queries.append(FixtureQuery(
            f"{topic}-typo", " ".join(typos), "typo",
            {f"{topic}-00": 3, f"{topic}-01": 1}))

    for i, text in enumerate(NO_ANSWER):
        queries.append(FixtureQuery(f"no-answer-{i}", text, "no-answer", {}))

    return docs, queries


if __name__ == "__main__":
    d, q = build_fixture()
    print(f"{len(d)} docs, {len(q)} queries "
          f"({len(set(x.category for x in q))} categories)")
