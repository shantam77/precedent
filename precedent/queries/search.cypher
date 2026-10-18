// Word search over decision titles and rationales, using the full-text index made in db.py. Best matches first.
CALL db.idx.fulltext.queryNodes('Decision', $q) YIELD node, score

RETURN node.id, node.title, node.status, node.trust, score

ORDER BY score DESC LIMIT 8
