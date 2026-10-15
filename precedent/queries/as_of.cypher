// Decisions that were in force on a date: started before it and not ended before it (missing valid_to = still true).
MATCH (d:Decision)-[:ABOUT]->(x)
WHERE d.valid_from <= $t AND (d.valid_to IS NULL OR d.valid_to > $t)
  AND ((x:Component AND x.name = $component) OR x:Repo OR $component = '*')
RETURN d.id, d.title, d.trust, d.valid_from, d.valid_to ORDER BY d.valid_from DESC LIMIT 25
