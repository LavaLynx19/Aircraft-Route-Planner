package api

import "strings"

// luceneSpecial lists the characters Lucene's query parser treats as syntax.
// Mirrors org.apache.lucene.queryparser.classic.QueryParserBase.escape. The
// backslash is first so the set can be scanned as a plain character set.
const luceneSpecial = `\+-!():^[]"{}~*?|&/`

// escapeLuceneQuery makes a user string safe to hand to db.index.fulltext.queryNodes.
//
// The query is already passed as a Cypher *parameter*, so there is no Cypher
// injection surface — but Lucene parses the parameter's value itself, so a stray
// `(`, `/` or `"` is a malformed expression, the driver returns an error, and the
// handler answered 500 SEARCH_ERROR for what is plainly bad client input
// (2026-07-05 review M1). Searching for `O'Hare`, `Côte d'Azur` or `Washington
// (Dulles)` failed that way.
//
// Escaping rather than rejecting is deliberate: ARCHITECTURE §8.1 documents `q` as
// a plain search query (`london`, `LHR`) and promises no operator syntax, so no
// caller can be relying on wildcards. Treating the input as literal text is
// therefore behaviour-preserving for every documented use and turns a 500 into a
// result.
func escapeLuceneQuery(q string) string {
	var b strings.Builder
	b.Grow(len(q) + 8)
	for _, r := range q {
		if strings.ContainsRune(luceneSpecial, r) {
			b.WriteByte('\\')
		}
		b.WriteRune(r)
	}
	return b.String()
}

// normalizeCode canonicalises an IATA/ICAO/family identifier from a query string.
//
// Graph.Airports is keyed by uppercase IATA and AliasToIATA by the uppercase and
// numeric identifiers Neo4j holds, but both were looked up with the raw parameter,
// so `from=lhr` and `aircraft=a380` returned 404 (2026-07-05 review M5). TrimSpace
// is included because `from=%20LHR` failed in exactly the same way.
//
// Applied only to code-shaped parameters. Country names are not normalised here:
// they are matched against Graph.ByCountry, whose keys are human-readable names
// where case is meaningful, and case-insensitive country lookup is a separate
// concern from this fix.
func normalizeCode(s string) string {
	return strings.ToUpper(strings.TrimSpace(s))
}
