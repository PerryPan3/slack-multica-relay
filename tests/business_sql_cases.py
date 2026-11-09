from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from business_sql import compile_sql,project_result
from review_evidence import Unavailable

class ScopedSQLTests(unittest.TestCase):
    op={"database":"dw","cluster":"cluster","schemas":["dwd","dim"]}
    material="dw.dwd.events dw.dim.shops"
    def query(self,sql,**overrides):
        args={"sql":sql,"start":"2026-11-01","end":"2026-11-05",**overrides}
        return compile_sql(self.op,args,self.material,[])
    def test_join_uses_each_bounded_relation_even_under_or(self):
        q="""WITH s AS (SELECT DISTINCT company_id FROM dim.shops WHERE p_hour=23)
        SELECT COUNT(*) AS n, COUNT(s.company_id) AS matched FROM dwd.events e
        LEFT JOIN s ON e.company_id=s.company_id WHERE e.p_hour=23 OR e.p_hour=22"""
        args,cols=self.query(q)
        self.assertEqual(cols,{"n":"number","matched":"number"})
        self.assertEqual(args["sql"].count('"pt" >= \'2026-11-01\''),2)
        self.assertEqual(args["sql"].count('"pt" < \'2026-11-06\''),2)
        self.assertTrue(args["sql"].endswith("LIMIT 32"))
    def test_discovery_scoped_to_database(self):
        q="SELECT COUNT(*) AS n FROM dim.orders"
        item={"name":"redshift_flow_dw_find_tables","database":"another","result":{"ok":True,"data":{"records":[{"schema":"dim","table":"orders"}]}}}
        args={"sql":q,"start":"2026-10-01","end":"2026-10-01"}
        with self.assertRaises(Unavailable):compile_sql(self.op,args,"", [item])
        item["database"]="dw"
        self.assertIn('FROM dim.orders',compile_sql(self.op,args,"",[item])[0]["sql"])
        with self.assertRaises(Unavailable):compile_sql(self.op,args,"another.dim.orders",[])
    def test_date_groups_and_lower_limit(self):
        _,cols=self.query("SELECT CAST(pt AS DATE) AS day, COUNT(DISTINCT company_id) AS companies FROM dim.shops GROUP BY 1 LIMIT 3")
        self.assertEqual(cols,{"day":"day","companies":"number"})
        with self.assertRaises(Unavailable):self.query("SELECT COUNT(*) AS n FROM dim.shops",end="2027-01-01")
    def test_rejects_writes_unverified_sources_and_unsafe_reads(self):
        invalid=[
            "DELETE FROM dim.shops","SELECT COUNT(*) AS n INTO dim.temp FROM dim.shops",
            "SELECT COUNT(*) AS n FROM dim.shops; DROP TABLE dim.shops",
            "WITH s AS (DELETE FROM dim.shops) SELECT COUNT(*) AS n FROM s",
            "UNLOAD ('SELECT 1') TO 's3://destination/'","COPY dim.shops FROM 's3://source/'",
            "SELECT email AS x FROM dim.shops","SELECT COUNT(*) OVER () AS n FROM dim.shops",
            "SELECT MAX(email) AS x FROM dim.shops","SELECT LISTAGG(email, ',') AS x FROM dim.shops",
            "SELECT COUNT(pg_sleep(2)) AS n FROM dim.shops","SELECT SUM(other.my_udf(company_id)) AS n FROM dim.shops",
            "SELECT COUNT(*) AS n FROM pg_catalog.pg_tables","SELECT COUNT(*) AS n FROM other.dim.shops",
            "SELECT COUNT(*) AS n FROM dwd.not_requested",
            "SELECT COUNT(*) AS n FROM dim.shops WHERE EXISTS (SELECT 1 FROM secret.payroll)",
            "SELECT COUNT(*) AS n FROM dim.shops a CROSS JOIN dwd.events b",
            "SELECT COUNT(*) AS n FROM dim.shops a JOIN dwd.events b ON a.company_id=a.company_id",
            "SELECT COUNT(*) AS n FROM dim.shops a JOIN dwd.events b ON a.company_id=b.company_id OR 1=1",
            "WITH RECURSIVE r AS (SELECT company_id FROM dim.shops) SELECT COUNT(*) AS n FROM r",
            "SELECT COUNT(*) AS n FROM shops","SELECT COUNT(*) AS n FROM dim.shops GROUP BY email",
        ]
        for sql in invalid:
            with self.subTest(sql=sql),self.assertRaises(Unavailable):self.query(sql)
    def test_transport_refuses_a_write_before_calling_mcp(self):
        import tempfile
        from unittest.mock import patch,AsyncMock
        import business_investigation as business
        cfg={"team":"T","owner":"O","self_dm":"D1","channel":"C","senders":["K"],"operations":{
            "redshift_flow_dw_aggregate_sql":{**self.op,"kind":"aggregate_sql"}}}
        claim={"business":True,"binding":["T","C","1","2","K","O","dw.dwd.events"],"history":[]}
        args={"sql":"DELETE FROM dwd.events","start":"2026-11-01","end":"2026-11-01"}
        with tempfile.TemporaryDirectory() as d:
            private=Path(d);(private/"review").mkdir();(private/"review/key").write_text("fixture")
            with patch.object(business.evidence,"PRIVATE",private), patch("slack_intent.verified",return_value=claim), patch.object(business,"load_policy",return_value=cfg), patch.object(business.evidence,"invoke",new_callable=AsyncMock) as upstream:
                with self.assertRaises(Unavailable):
                    business.execute({"args":{},"history":[],"request":{"name":"redshift_flow_dw_aggregate_sql","arguments":args}})
                upstream.assert_not_awaited()

    def test_return_plane_rejects_details(self):
        for data in [{"columns":["n"],"rows":[["private@example.com"]]}, {"columns":["email"],"rows":[[1]]}]:
            with self.assertRaises(Unavailable):project_result(data,{"n":"number"})
        out=project_result({"columns":["n"],"rows":[[1]]*32},{"n":"number"})
        self.assertTrue(out["truncated"]);self.assertEqual(len(out["records"]),31)

if __name__=="__main__":unittest.main()
