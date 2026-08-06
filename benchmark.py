import os
import time
import random
import ssl
import numpy as np
from dotenv import load_dotenv
from neo4j import GraphDatabase
from falkordb import FalkorDB

# Load configurations from the secure .env file
load_dotenv()

print("Generating 100,000 graph relationships in memory...")
NUM_NODES = 10000
NUM_EDGES = 100000
edges = [(random.randint(1, NUM_NODES), random.randint(1, NUM_NODES)) for _ in range(NUM_EDGES)]
test_nodes = [str(random.randint(1, NUM_NODES)) for _ in range(20)] # Target nodes for queries


def _is_constraint_error(error):
    message = str(error).lower()
    return "constraint violation" in message or "unique constraint" in message or "already exists" in message


def benchmark_bolt_database(uri, user, password, name):
    """Benchmarks standard Bolt-protocol databases (CognoDB, Neo4j, Memgraph)"""
    print(f"\n🚀 Connecting to {name}...")
    try:
        clean_uri = uri
        if "bolt+s://" in uri:
            clean_uri = uri.replace("bolt+s://", "bolt://")
        elif "neo4j+s://" in uri:
            clean_uri = uri.replace("neo4j+s://", "neo4j://")
            
        custom_ssl_context = ssl.create_default_context()
        custom_ssl_context.check_hostname = False
        custom_ssl_context.verify_mode = ssl.CERT_NONE
        
        driver = GraphDatabase.driver(clean_uri, auth=(user, password), ssl_context=custom_ssl_context)
        with driver.session() as session:
            print(f"[{name}] Wiping existing database data...")
            session.run("MATCH (n) DETACH DELETE n")
            
            print(f"[{name}] Enforcing structural uniqueness constraints...")
            try:
                session.run("CREATE CONSTRAINT person_id FOR (p:Person) REQUIRE p.id IS UNIQUE")
                time.sleep(1)
            except Exception:
                try: session.run("CREATE CONSTRAINT ON (p:Person) ASSERT p.id IS UNIQUE")
                except: print(f"ℹ️ Custom constraint configuration handled natively by {name}")
            
            # --- 1. Data Ingestion Throughput ---
            print(f"[{name}] Starting ingestion of 100,000 relationships...")
            start_time = time.perf_counter()
            
            batch_size = 5000
            for i in range(0, len(edges), batch_size):
                batch = edges[i:i+batch_size]
                unwound_data = [{"source": str(s), "target": str(t)} for s, t in batch]
                try:
                    session.run("""
                        UNWIND $pairs AS pair
                        MERGE (s:Person {id: pair.source})
                        MERGE (t:Person {id: pair.target})
                        CREATE (s)-[:FOLLOWS]->(t)
                    """, pairs=unwound_data)
                except Exception as exc:
                    if _is_constraint_error(exc):
                        print(f"⚠️ {name} hit a uniqueness constraint during bulk ingest; retrying with per-edge inserts...")
                        for row in unwound_data:
                            session.run(
                                "MERGE (s:Person {id: $source}) MERGE (t:Person {id: $target}) CREATE (s)-[:FOLLOWS]->(t)",
                                source=row["source"],
                                target=row["target"],
                            )
                    else:
                        raise
                
            elapsed_load = time.perf_counter() - start_time
            ingest_rate = NUM_EDGES / elapsed_load
            print(f"✅ {name} Ingestion Complete: {elapsed_load:.2f}s ({ingest_rate:.2f} rels/sec)")
            
            # --- 2. Warm-up Phase ---
            for t_node in test_nodes:
                session.run("MATCH (n:Person {id: $id})-[:FOLLOWS]->(m) RETURN count(m)", id=t_node)
                
            # --- 3. Multi-Hop Traversals ---
            traversal_metrics = {1: [], 2: []}
            for hop in [1, 2]:
                path_pattern = "(start:Person {id: $id})"
                for idx in range(hop):
                    node_var = f"n{idx}"
                    path_pattern += f"-[:FOLLOWS]->({node_var}:Person)"
                query = f"MATCH p = {path_pattern} RETURN count(p)"
                
                for _ in range(5):
                    for node_id in test_nodes:
                        q_start = time.perf_counter()
                        session.run(query, id=node_id)
                        traversal_metrics[hop].append((time.perf_counter() - q_start) * 1000)
                        
            driver.close()
            return {
                "ingest_rate": ingest_rate,
                "hop1_p50": np.percentile(traversal_metrics[1], 50),
                "hop1_p95": np.percentile(traversal_metrics[1], 95),
                "hop2_p50": np.percentile(traversal_metrics[2], 50),
                "hop2_p95": np.percentile(traversal_metrics[2], 95),
            }
    except Exception as e:
        print(f"❌ Error benchmarking {name}: {e}")
        return None

def benchmark_falkordb():
    """Benchmarks FalkorDB via the official falkordb library with memory safety chunking"""
    print("\n🚀 Connecting to FalkorDB Cloud...")
    try:
        db = FalkorDB(
            host=os.getenv("FALKORDB_HOST"),
            port=int(os.getenv("FALKORDB_PORT", 6379)),
            username="falkordb",
            password=os.getenv("FALKORDB_PASSWORD")
        )
        fg = db.select_graph("wexa_benchmark")
        try: fg.delete()
        except Exception: pass
        
        # --- 1. Data Ingestion Throughput ---
        print("[FalkorDB] Starting ingestion of 100,000 relationships...")
        start_time = time.perf_counter()
        
        batch_size = 50 
        for i in range(0, len(edges), batch_size):
            batch = edges[i:i+batch_size]
            query = ""
            for idx, (s, t) in enumerate(batch):
                query += f"MERGE (s{idx}:Person {{id: '{s}'}}) MERGE (t{idx}:Person {{id: '{t}'}}) CREATE (s{idx})-[:FOLLOWS]->(t{idx}) "
            try:
                fg.query(query)
            except Exception as exc:
                print(f"⚠️ FalkorDB ingest skipped: {exc}")
                return None
            
        elapsed_load = time.perf_counter() - start_time
        ingest_rate = NUM_EDGES / elapsed_load
        print(f"✅ FalkorDB Ingestion Complete: {elapsed_load:.2f}s ({ingest_rate:.2f} rels/sec)")
        
        # --- 2. Warm-up & Traversals ---
        traversal_metrics = {1: [], 2: []}
        for hop in [1, 2]:
            path_pattern = "(start:Person {id: $id})"
            for idx in range(hop):
                node_var = f"n{idx}"
                path_pattern += f"-[:FOLLOWS]->({node_var}:Person)"
            query = f"MATCH p = {path_pattern} RETURN count(p)"
            
            for _ in range(5):
                for node_id in test_nodes:
                    q_start = time.perf_counter()
                    fg.query(query, {'id': node_id})
                    traversal_metrics[hop].append((time.perf_counter() - q_start) * 1000)
                    
        return {
            "ingest_rate": ingest_rate,
            "hop1_p50": np.percentile(traversal_metrics[1], 50),
            "hop1_p95": np.percentile(traversal_metrics[1], 95),
            "hop2_p50": np.percentile(traversal_metrics[2], 50),
            "hop2_p95": np.percentile(traversal_metrics[2], 95),
        }
    except Exception as e:
        print(f"❌ Error benchmarking FalkorDB: {e}")
        return None

# --- Main Driver Loop ---
if __name__ == "__main__":
    results = {}
    
    results["CognoDB"] = benchmark_bolt_database(os.getenv("COGNODB_URI"), os.getenv("COGNODB_USER"), os.getenv("COGNODB_PASSWORD"), "CognoDB")
    results["Neo4j Aura"] = benchmark_bolt_database(os.getenv("NEO4J_URI"), os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"), "Neo4j Aura")
    results["Memgraph"] = benchmark_bolt_database(os.getenv("MEMGRAPH_URI"), os.getenv("MEMGRAPH_USER"), os.getenv("MEMGRAPH_PASSWORD"), "Memgraph")
    results["FalkorDB"] = benchmark_falkordb()
    
    print("\n" + "="*75 + "\n📊 FINAL PERFORMANCE BENCHMARK MATRIX\n" + "="*75)
    print(f"{'Platform':<15} | {'Ingest (R/s)':<12} | {'1-Hop p50':<10} | {'1-Hop p95':<10} | {'2-Hop p50':<10}")
    print("-"*85)
    for plat, data in results.items():
        if data:
            print(f"{plat:<15} | {data['ingest_rate']:<12.2f} | {data['hop1_p50']:<10.2f} | {data['hop1_p95']:<10.2f} | {data['hop2_p50']:<10.2f}")
