// Search decisions by words in their title or reason, best matches first.
CALL db.idx.fulltext.queryNodes('Decision', $q) YIELD node, score

RETURN node.id, node.title, node.status, node.trust, score

ORDER BY score DESC LIMIT 8
