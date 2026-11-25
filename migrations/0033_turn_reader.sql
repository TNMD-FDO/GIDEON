-- Each later table the turn reads needs its own grant in a later migration.

GRANT USAGE ON SCHEMA public TO gideon_ro_turn;
GRANT SELECT ON TABLE index_generations, documents, opinions, anchors, citations, cluster_treatment
    TO gideon_ro_turn;
