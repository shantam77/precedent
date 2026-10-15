// Full-text search over decision titles and rationales (FalkorDB full-text index, BM25-style).
CALL db.idx.fulltext.queryNodes('Decision', $q) YIELD node, score
RETURN node.id, node.title, node.status, node.trust, score ORDER BY score DESC LIMIT 8
