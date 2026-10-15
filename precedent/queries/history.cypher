// Recent changes to the graph (the graph's own git log), newest first, optionally excluding one session.
MATCH (s:Session)-[:MADE]->(ch:Change)
WHERE ch.at >= $since AND s.id <> $exclude
OPTIONAL MATCH (ch)-[:ON]->(d:Decision)
RETURN ch.id, ch.at, ch.op, ch.actor, s.harness, ch.reason, collect(d.id) ORDER BY ch.at DESC LIMIT $limit
