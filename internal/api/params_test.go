package api

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func TestEscapeLuceneQuery(t *testing.T) {
	// Unit-tested directly rather than through the handler: both search endpoints
	// need a live Neo4j driver and the fixture server passes nil, so there is no
	// handler-level route to this code.
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"plain text is untouched", "london", "london"},
		{"codes are untouched", "LHR", "LHR"},
		{"spaces are not special", "new york", "new york"},
		{"apostrophe is not a Lucene metacharacter", "O'Hare", "O'Hare"},
		{"parenthesis escaped", "Washington (Dulles)", `Washington \(Dulles\)`},
		{"slash escaped", "Kingsford/Smith", `Kingsford\/Smith`},
		{"quote escaped", `say "hi"`, `say \"hi\"`},
		{"wildcards escaped, not honoured", "lon*", `lon\*`},
		{"question mark escaped", "LH?", `LH\?`},
		{"colon escaped", "name:london", `name\:london`},
		{"backslash escaped", `a\b`, `a\\b`},
		{"boolean operators escaped", "a && b || c", `a \&\& b \|\| c`},
		{"leading plus and minus escaped", "+a -b", `\+a \-b`},
		{"brackets and braces escaped", "a[1] b{2}", `a\[1\] b\{2\}`},
		{"caret and tilde escaped", "a^2 b~3", `a\^2 b\~3`},
		{"bang escaped", "a!b", `a\!b`},
		{"empty stays empty", "", ""},
		{"non-ascii is untouched", "Côte d'Azur", "Côte d'Azur"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := escapeLuceneQuery(tc.in); got != tc.want {
				t.Errorf("escapeLuceneQuery(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestEscapeLuceneQueryEscapesEverySpecialCharacter(t *testing.T) {
	// Guards the set itself: if a character is dropped from luceneSpecial, the
	// table above might still pass while that character starts causing 500s again.
	for _, r := range luceneSpecial {
		in := string(r)
		want := `\` + in
		if got := escapeLuceneQuery(in); got != want {
			t.Errorf("escapeLuceneQuery(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestNormalizeCode(t *testing.T) {
	tests := []struct {
		in, want string
	}{
		{"lhr", "LHR"},
		{"LHR", "LHR"},
		{"a380", "A380"},
		{"a388", "A388"},
		{"388", "388"},
		{" LHR ", "LHR"},
		{"\tsyd\n", "SYD"},
		{"", ""},
	}
	for _, tc := range tests {
		if got := normalizeCode(tc.in); got != tc.want {
			t.Errorf("normalizeCode(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestLowercaseIdentifiersAreAccepted(t *testing.T) {
	// Jul review M5: these all returned 404 because Graph.Airports is keyed by
	// uppercase IATA and AliasToIATA by the identifiers Neo4j holds, while the raw
	// query parameter was used for the lookup.
	srv := newFixtureServer()

	tests := []struct {
		name string
		url  string
	}{
		{"lowercase airports", "/routes?aircraft=388&from=lhr&to=syd&max_hops=2"},
		{"lowercase aircraft family", "/routes?aircraft=a380&from=LHR&to=SYD&max_hops=2"},
		{"lowercase icao code", "/routes?aircraft=a388&from=LHR&to=SYD&max_hops=2"},
		{"mixed case throughout", "/routes?aircraft=A380&from=Lhr&to=sYd&max_hops=2"},
		{"padded airport code", "/routes?aircraft=388&from=%20LHR&to=SYD&max_hops=2"},
		{"lowercase on routes/from", "/routes/from?aircraft=388&from=lhr&max_hops=2"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			code, body := get(t, srv, tc.url)
			if code != http.StatusOK {
				t.Fatalf("got %d, want 200; body: %s", code, body)
			}
			var resp struct {
				Data []json.RawMessage `json:"data"`
			}
			decode(t, body, &resp)
			if len(resp.Data) == 0 {
				t.Errorf("expected routes, got none: %s", body)
			}
		})
	}
}

func TestUnknownIdentifiersStillFail(t *testing.T) {
	// Normalisation must not turn a genuine 404 into a match.
	srv := newFixtureServer()
	for _, tc := range []struct {
		url  string
		code int
	}{
		{"/routes?aircraft=388&from=zzz&to=SYD&max_hops=2", http.StatusNotFound},
		{"/routes?aircraft=nope&from=LHR&to=SYD&max_hops=2", http.StatusNotFound},
	} {
		code, body := get(t, srv, tc.url)
		if code != tc.code {
			t.Errorf("%s: got %d, want %d; body: %s", tc.url, code, tc.code,
				strings.TrimSpace(string(body)))
		}
	}
}
