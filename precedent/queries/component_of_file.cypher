// Which component (folder group) a file belongs to. No row for a file the scan never saw: tools.component_of() then uses the longest matching folder.
MATCH (f:File {path: $path})-[i:IN]->(c:Component)

return c.name as name
