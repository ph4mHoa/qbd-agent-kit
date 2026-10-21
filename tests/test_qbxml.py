import unittest

from qbd_agent_kit.errors import QbxmlInputError
from qbd_agent_kit.qbxml.build import build_request, envelope
from qbd_agent_kit.qbxml.parse import col_index, parse_reports, parse_responses


class Build(unittest.TestCase):
    def test_query_children_come_out_in_schema_order_whatever_the_input_order(self):
        xml = build_request("InvoiceQueryRq", {
            "IncludeLineItems": True,
            "PaidStatus": "NotPaidOnly",
            "TxnDateRangeFilter": {"ToTxnDate": "2026-09-30", "FromTxnDate": "2026-09-01"},
            "MaxReturned": 10,
        }, {"requestID": "1"})
        self.assertEqual(
            xml,
            '<InvoiceQueryRq requestID="1"><MaxReturned>10</MaxReturned><TxnDateRangeFilter><FromTxnDate>2026-09-01</FromTxnDate>'
            "<ToTxnDate>2026-09-30</ToTxnDate></TxnDateRangeFilter><PaidStatus>NotPaidOnly</PaidStatus>"
            "<IncludeLineItems>true</IncludeLineItems></InvoiceQueryRq>",
        )

    def test_amounts_get_two_decimals_quantities_up_to_five_text_is_escaped(self):
        xml = build_request("InvoiceAddRq", {
            "InvoiceAdd": {
                "CustomerRef": "Smith & Sons",
                "TxnDate": "2026-10-20",
                "Memo": "Café Québec",
                "InvoiceLineAdd": [
                    {"ItemRef": {"FullName": "Widget"}, "Quantity": 2.5, "Rate": 10.123456},
                    {"ItemRef": "80000001-1234567890", "Amount": 5},
                ],
            },
        })
        self.assertIn("<CustomerRef><FullName>Smith &amp; Sons</FullName></CustomerRef>", xml)
        self.assertIn("<Memo>Caf&#233; Qu&#233;bec</Memo>", xml)
        self.assertIn("<Quantity>2.5</Quantity><Rate>10.12346</Rate>", xml)
        self.assertIn("<ItemRef><ListID>80000001-1234567890</ListID></ItemRef><Amount>5.00</Amount>", xml)

    def test_text_longer_than_quickbooks_takes_is_refused_before_it_is_sent(self):
        add = {"CustomerRef": "Alder Street Cafe", "RefNumber": "ZZ-QBAI001-1"}
        with self.assertRaisesRegex(QbxmlInputError, 'InvoiceAdd/RefNumber: "ZZ-QBAI001-1" has 12 characters; QuickBooks takes at most 11'):
            build_request("InvoiceAddRq", {"InvoiceAdd": add})
        self.assertIn("<RefNumber>ZZ-QBAI-001</RefNumber>", build_request("InvoiceAddRq", {"InvoiceAdd": {**add, "RefNumber": "ZZ-QBAI-001"}}))

    def test_unknown_elements_are_refused_with_the_allowed_list(self):
        with self.assertRaisesRegex(QbxmlInputError, r'unknown element "Nope".*MaxReturned'):
            build_request("CustomerQueryRq", {"Nope": 1})

    def test_a_single_value_where_the_schema_allows_one_is_enforced(self):
        with self.assertRaisesRegex(QbxmlInputError, "only one value"):
            build_request("InvoiceQueryRq", {"MaxReturned": [1, 2]})

    def test_envelope_wraps_requests_with_the_qbxml_version(self):
        xml = envelope(['<HostQueryRq requestID="1"></HostQueryRq>'], "16.0")
        self.assertIn('<?qbxml version="16.0"?>', xml)
        self.assertIn('<QBXMLMsgsRq onError="continueOnError"><HostQueryRq', xml)


class Parse(unittest.TestCase):
    def test_responses_are_normalized_lists_numbers_booleans_ids_as_text(self):
        xml = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
        <InvoiceQueryRs requestID="7" statusCode="0" statusSeverity="Info" statusMessage="Status OK" iteratorRemainingCount="3">
          <InvoiceRet><TxnID>12-1700000000</TxnID><RefNumber>00042</RefNumber><CustomerRef><ListID>80000001-1600000000</ListID><FullName>Acme</FullName></CustomerRef>
            <TxnDate>2026-09-01</TxnDate><BalanceRemaining>150.50</BalanceRemaining><IsPaid>false</IsPaid>
            <InvoiceLineRet><TxnLineID>1</TxnLineID><Quantity>3</Quantity><Amount>150.50</Amount></InvoiceLineRet>
          </InvoiceRet>
        </InvoiceQueryRs>
        <CustomerQueryRs requestID="8" statusCode="1" statusSeverity="Info" statusMessage="A query request did not find a matching object in QuickBooks"/>
      </QBXMLMsgsRs></QBXML>"""
        inv, cust = parse_responses(xml)
        self.assertEqual(inv.request_id, "7")
        self.assertEqual(inv.attrs["iteratorRemainingCount"], "3")
        rets = inv.body["InvoiceRet"]
        self.assertEqual(len(rets), 1)
        self.assertEqual(rets[0]["RefNumber"], "00042")
        self.assertEqual(rets[0]["BalanceRemaining"], 150.5)
        self.assertIs(rets[0]["IsPaid"], False)
        self.assertEqual(rets[0]["CustomerRef"]["ListID"], "80000001-1600000000")
        self.assertEqual(len(rets[0]["InvoiceLineRet"]), 1)
        self.assertEqual(cust.status_code, 1)

    def test_numeric_character_references_from_quickbooks_are_decoded(self):
        rs = parse_responses("""<QBXML><QBXMLMsgsRs><CustomerQueryRs requestID="1" statusCode="0" statusSeverity="Info" statusMessage="Status OK">
        <CustomerRet><ListID>1</ListID><Name>Qu&#233;bec &amp; Fils</Name></CustomerRet></CustomerQueryRs></QBXMLMsgsRs></QBXML>""")
        self.assertEqual(rs[0].body["CustomerRet"][0]["Name"], "Québec & Fils")
        [r] = parse_reports("""<QBXML><QBXMLMsgsRs><GeneralSummaryReportQueryRs requestID="1" statusCode="0" statusSeverity="Info" statusMessage="Status OK">
        <ReportRet><ReportTitle>Balance Sheet</ReportTitle><ColDesc colID="1" dataType="STRTYPE"><ColTitle titleRow="1" /><ColType>Label</ColType></ColDesc>
        <ReportData><DataRow rowNumber="1"><RowData rowType="account" value="Financi&#232;re &amp;#1;" /><ColData colID="1" value="Financi&#232;re &amp;#1;" /></DataRow></ReportData>
        </ReportRet></GeneralSummaryReportQueryRs></QBXMLMsgsRs></QBXML>""")
        self.assertEqual(r.rows[0].label, "Financière &#1;")

    def test_report_rows_keep_their_order_and_get_depth_from_text_and_subtotal_rows(self):
        xml = """<QBXML><QBXMLMsgsRs><GeneralSummaryReportQueryRs requestID="1" statusCode="0" statusSeverity="Info" statusMessage="Status OK">
      <ReportRet><ReportTitle>Profit &amp; Loss</ReportTitle><ReportSubtitle>September 2026</ReportSubtitle><ReportBasis>Accrual</ReportBasis>
      <NumRows>5</NumRows><NumColumns>2</NumColumns><NumColTitleRows>1</NumColTitleRows>
      <ColDesc colID="1" dataType="STRTYPE"><ColTitle titleRow="1" /><ColType>Label</ColType></ColDesc>
      <ColDesc colID="2" dataType="AMTTYPE"><ColTitle titleRow="1" value="TOTAL" /><ColType>Total</ColType></ColDesc>
      <ReportData>
        <TextRow rowNumber="1" value="Income" />
        <DataRow rowNumber="2"><RowData rowType="account" value="Sales" /><ColData colID="1" value="Sales" /><ColData colID="2" value="1000.00" /></DataRow>
        <SubtotalRow rowNumber="3"><RowData rowType="account" value="Income" /><ColData colID="1" value="Total Income" /><ColData colID="2" value="1000.00" /></SubtotalRow>
        <TotalRow rowNumber="4"><ColData colID="1" value="Net Income" /><ColData colID="2" value="400.00" /></TotalRow>
      </ReportData></ReportRet></GeneralSummaryReportQueryRs></QBXMLMsgsRs></QBXML>"""
        [r] = parse_reports(xml)
        self.assertEqual(r.title, "Profit & Loss")
        self.assertEqual(r.basis, "Accrual")
        self.assertEqual([[x.kind, x.depth, x.label] for x in r.rows], [
            ["text", 0, "Income"],
            ["data", 1, "Sales"],
            ["subtotal", 0, "Income"],
            ["total", 0, "Net Income"],
        ])
        self.assertEqual(r.rows[1].values[col_index(r, r"^total\Z")], 1000)
        self.assertEqual(r.rows[3].values, ["Net Income", 400])


if __name__ == "__main__":
    unittest.main()
