from config import neo4j_config
from neo4j import GraphDatabase

CONSTRAINTS = [
    # Uniqueness constraints (also create indexes)
    """CREATE CONSTRAINT airport_iata IF NOT EXISTS
       FOR (a:Airport) REQUIRE a.iata IS UNIQUE""",
    """CREATE CONSTRAINT aircraft_iata IF NOT EXISTS
       FOR (ac:Aircraft) REQUIRE ac.iata IS UNIQUE""",
    # Relationship property index for aircraft-filtered traversal
    """CREATE INDEX route_aircraft IF NOT EXISTS
       FOR ()-[r:ROUTE]-() ON (r.aircraft)""",
    # Full-text indexes for search endpoints
    """CREATE FULLTEXT INDEX airport_search IF NOT EXISTS
       FOR (a:Airport) ON EACH [a.name, a.city, a.iata]""",
    """CREATE FULLTEXT INDEX aircraft_search IF NOT EXISTS
       FOR (ac:Aircraft) ON EACH [ac.name, ac.iata, ac.icao, ac.family]""",
]


def create_constraints(driver):
    with driver.session() as session:
        for cypher in CONSTRAINTS:
            session.run(cypher)
            label = cypher.strip().split("\n")[0].strip()
            print(f"  ✓ {label}")
    print(f"Created {len(CONSTRAINTS)} constraints/indexes.")


if __name__ == "__main__":
    cfg = neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        create_constraints(driver)
    finally:
        driver.close()
