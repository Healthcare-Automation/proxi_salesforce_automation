"""Verify the parser for SF DUPLICATE_VALUE errors used by the PATCH retry."""

from utils.sf_job_rest_minimal import parse_duplicate_value_error


def test_parses_canonical_patch_message():
    err = Exception(
        "Salesforce REST PATCH HTTP 400: duplicate value found: "
        "Job_Client_Job_Id__c duplicates value on record with id: a015f00000KxRpaAAF"
    )
    assert parse_duplicate_value_error(err) == (
        "Job_Client_Job_Id__c",
        "a015f00000KxRpaAAF",
    )


def test_parses_18_char_record_id():
    err = Exception(
        "duplicate value found: External_Job_ID__c duplicates value on record with id: "
        "a01UP00000fw2ORYAY"
    )
    assert parse_duplicate_value_error(err) == ("External_Job_ID__c", "a01UP00000fw2ORYAY")


def test_returns_none_for_deleted_entity_error():
    err = Exception("Salesforce REST PATCH HTTP 400: entity is deleted")
    assert parse_duplicate_value_error(err) is None


def test_returns_none_for_arbitrary_error():
    assert parse_duplicate_value_error(Exception("network unreachable")) is None


def test_case_insensitive():
    err = Exception(
        "DUPLICATE VALUE FOUND: My_Field__c DUPLICATES VALUE ON RECORD WITH ID: a01UP00000fw2ORYAY"
    )
    assert parse_duplicate_value_error(err) == ("My_Field__c", "a01UP00000fw2ORYAY")
