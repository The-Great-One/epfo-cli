"""The profile and service-history pages.

Both are static server-rendered pages: every value is already in the markup, so
these fixtures are real captured markup with the personal values replaced. Each
test pins a structural quirk that a page-wide sweep gets wrong.
"""

from __future__ import annotations

from epfo.models import label_values, parse_profile, parse_service_history

# Real markup shape: one timeline item per establishment, each repeating the
# same labels. A page-wide label sweep collapses all four into one set.
SERVICE_HISTORY = """
<div class="profile-sub-heading">Service Overview</div>
<div class="row">
  <div class="col-sm-5"><p class="mb-0 bold texth">Total Experience</p></div>
  <div class="col-sm-7"><p class="text-muted mb-0">
      <span class="border-text">4 Years 1 Months 0 Days</span></p></div>
</div>
<div class="row">
  <div class="col-sm-5"><p class="mb-0 bold texth">Date of Joining</p></div>
  <div class="col-sm-7"><p class="text-muted mb-0">
      <span class="border-text">04-Jul-2022</span></p></div>
</div>
<div class="profile-sub-heading">Member Id wise Service Length</div>
<div class="profile-sub-heading">Service History</div>
<ul class="timeline">
  <li class="timeline-item mb-3">
    <div class="service-history-heading">1</div>
    <p class="tlp tlp-green">May 2026 - Present</p>
    <p class="text-muted mb-2 bold bhashini-skip-translation">ACME PRIVATE LIMITED</p>
    <div class="col-md-4"><p class="mb-0">Est Id</p></div>
    <div class="col-md-8"><p class="mb-0 text-md-start">EST123456</p></div>
    <div class="col-md-4"><p class="mb-0">Member Id</p></div>
    <div class="col-md-8"><p class="mb-0 text-md-start">AAAAA0000000000000001</p></div>
    <div class="col-md-4"><p class="mb-0">NCP Days</p></div>
    <div class="col-md-8"><p class="mb-0 text-md-start">0 Days</p></div>
    <div class="col-md-4"><p class="mb-0">Joining Date</p></div>
    <div class="col-md-8"><p class="mb-0 text-md-start">01-May-2026&nbsp;
        <span class="fa fa-calendar"></span></p></div>
    <div class="col-md-4"><p class="mb-0">Total Service</p></div>
    <div class="col-md-8"><p class="mb-0">0 Years 5 Months 5 Days</p></div>
  </li>
  <li class="timeline-item mb-3">
    <div class="service-history-heading">2</div>
    <p class="tlp tlp-green">04-Jul-2022 - 02-Dec-2024</p>
    <p class="text-muted mb-2 bold bhashini-skip-translation">OLD EMPLOYER LIMITED</p>
    <div class="col-md-4"><p class="mb-0">Est Id</p></div>
    <div class="col-md-8"><p class="mb-0 text-md-start">EST999999</p></div>
    <div class="col-md-4"><p class="mb-0">Member Id</p></div>
    <div class="col-md-8"><p class="mb-0 text-md-start">BBBBB0000000000000002</p></div>
    <div class="col-md-4"><p class="mb-0">Joining Date</p></div>
    <div class="col-md-8"><p class="mb-0 text-md-start">04-Jul-2022&nbsp;
        <span class="fa fa-calendar"></span></p></div>
    <div class="col-md-4"><p class="mb-0">Exit Date</p></div>
    <div class="col-md-8"><p class="mb-0 text-md-start">02-Dec-2024</p></div>
    <div class="col-md-4"><p class="mb-0">Total Service</p></div>
    <div class="col-md-8"><p class="mb-0">2 Years 4 Months 28 Days</p></div>
  </li>
</ul>
"""

PROFILE = """
<div class="profile-sub-heading">Home</div>
<div class="profile-sub-heading">Profile</div>
<div class="profile-sub-heading">Passbook</div>
<div class="profile-sub-heading">Calculators</div>
<div class="profile-sub-heading">Logout</div>
<div class="profile-sub-heading">Basic Details</div>
<div class="row">
  <div class="col-sm-4"><p class="mb-0">Full Name</p></div>
  <div class="col-sm-8"><p class="text-muted mb-0">TEST USER</p></div>
</div>
<div class="row">
  <div class="col-sm-4"><p class="mb-0">Date of Birth</p></div>
  <div class="col-sm-8"><p class="text-muted mb-0">01-01-2000
      <span class="fa fa-calendar"></span></p></div>
</div>
<div class="row">
  <div class="col-sm-4"><p class="mb-0">Aadhaar</p></div>
  <div class="col-sm-8"><p class="text-muted mb-0">000000000000</p></div>
</div>
<div class="profile-sub-heading">Visitor Count</div>
"""


def test_each_establishment_is_parsed_separately():
    # The same labels repeat in every timeline item. A page-wide label sweep
    # returns one establishment's values for all of them.
    history = parse_service_history(SERVICE_HISTORY)
    assert [r.employer for r in history.records] == [
        "ACME PRIVATE LIMITED", "OLD EMPLOYER LIMITED"]
    assert [r.member_id for r in history.records] == [
        "AAAAA0000000000000001", "BBBBB0000000000000002"]
    assert [r.establishment_id for r in history.records] == [
        "EST123456", "EST999999"]


def test_service_overview_totals_are_read():
    history = parse_service_history(SERVICE_HISTORY)
    assert history.total_experience == "4 Years 1 Months 0 Days"
    assert history.date_of_joining == "04-Jul-2022"


def test_calendar_icon_does_not_leak_into_dates():
    history = parse_service_history(SERVICE_HISTORY)
    assert history.records[0].joining_date == "01-May-2026"
    assert history.records[1].exit_date == "02-Dec-2024"


def test_a_present_role_takes_its_exit_from_the_period():
    # The current role has no Exit Date cell at all.
    history = parse_service_history(SERVICE_HISTORY)
    assert history.records[0].exit_date == "Present"


def test_a_page_without_a_timeline_is_reported_not_guessed():
    history = parse_service_history("<div>nothing</div>")
    assert history.records == []
    assert history.notes


def test_profile_skips_the_navigation_labels():
    # The nav entries use the same label/value markup as real fields.
    fields = parse_profile(PROFILE)
    for chrome in ("Home", "Profile", "Passbook", "Calculators", "Logout"):
        assert chrome not in fields
    assert fields["Full Name"] == "TEST USER"
    assert fields["Aadhaar"] == "000000000000"


def test_profile_stops_before_the_footer():
    fields = parse_profile(PROFILE)
    assert "Visitor Count" not in fields


def test_label_values_pairs_label_with_the_adjacent_value():
    pairs = label_values('<p class="mb-0 bold texth">Total NCP Days</p>'
                         '<div class="col-sm-7"><p class="text-muted mb-0">'
                         '<span class="border-text">0 Days</span></p></div>')
    assert pairs["Total NCP Days"] == "0 Days"
