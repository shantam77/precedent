// Decisions that were in force at time $t: started by then and not ended by then (no valid_to = still in force).
// Repo-wide decisions always count; '*' means every component.
MATCH (d:Decision)-[:ABOUT]->(x)
WHERE d.valid_from <= $t
  AND (d.valid_to IS NULL OR d.valid_to > $t)
  AND ((x:Component AND x.name = $component) OR x:Repo OR $component = "*")
RETURN d.id, d.title, d.trust, d.valid_from, d.valid_to
ORDER BY d.valid_from DESC LIMIT 25
