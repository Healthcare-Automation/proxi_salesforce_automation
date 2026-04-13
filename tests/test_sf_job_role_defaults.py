"""Job role defaults: PATCH only when Salesforce field is empty."""

from utils.sf_job_payload import merge_job_role_defaults_for_empty_sf_fields


def test_merge_fills_all_when_current_empty():
    target: dict = {}
    merge_job_role_defaults_for_empty_sf_fields(target, {})
    assert target.get("Job_Position_Type__c") == "Locums"
    assert target.get("Job_Specialty__c") == "General Dentistry"
    assert target.get("Occupation_DJC__c") == "Dentist"
    assert target.get("Job_Job_Source__c") == "Shiftwise - Aspen Dental - AMN"


def test_merge_skips_when_salesforce_has_value():
    target: dict = {}
    current = {
        "Job_Position_Type__c": "Permanent",
        "Job_Specialty__c": "",
        "Occupation_DJC__c": None,
        "Job_Job_Source__c": "Other Source",
    }
    merge_job_role_defaults_for_empty_sf_fields(target, current)
    assert "Job_Position_Type__c" not in target
    assert "Job_Job_Source__c" not in target
    assert target.get("Job_Specialty__c") == "General Dentistry"
    assert target.get("Occupation_DJC__c") == "Dentist"
