# What the real data confirmed (October 2026)

Source: `tools/sql/confirm_match_keys.sql`, run by the data owner in CDERONE against
`ctp_pd_catalog` (results in `sql outputs/`). Check authors should treat these as settled.
Section codes (K01, P2.11, ...) refer to rows and results in that script.

## How the systems link

| Link | Result | Use |
|---|---|---|
| Rhapsody product ID ↔ PMS | `product_id` = `pms_product.uuid`: 100% (K24, K50). `pms_product.id` 0% (K22, K23, K29, K49). Static IDs 0.1% (K27). | Match Rhapsody products to PMS on **uuid only**. |
| PMS ↔ Reporting products | `pms_product.id` = `dim_product.id`: 100% (K34). Facts use the same id (K35, K36). | Reporting product IDs are PMS ids. A Rhapsody product reaches Reporting through `dim_product.uuid`. Rhapsody bulk IDs vs `dim_product.id` match 0% (K64). |
| TP number Rhapsody ↔ PMS | 100% (K51) | Reliable secondary key. |
| Rhapsody ↔ eSUB submissions | STN vs active log-ins 93.8%, vs active **and inactive** 99.9% (K06, K08); `reglist_key` 100% (K09); both together 99.9% (K10). | About 2,900 Rhapsody STNs exist only on inactive eSUB log-ins. |
| eSUB log-in ↔ electronic submission | Log-in `STN` = `SUBMISSION_TRACKING_NUMBER`: 99.8% (K02). Log-in `TRACKING_NUMBER` is a different number: 0% (K01). `PACKAGE_KEY`: 100% of electronic submissions (K03). | |
| eSUB `STN_LINK` | **Not an STN**: 0 of 120,700 values match any STN (K39). | Never treat it as an STN. |
| eSUB `STN_BEING_AMENDED` | Never yields an STN when split (K40). | No check can use it. |
| Company FEI eSUB ↔ Rhapsody | No STN has an FEI in both systems (K66). | No FEI comparison is possible. |
| Rhapsody letters → bundles | `uploadcommunicationstobundle_id` reaches 97.4% of bundles (K21); `bundleid` 75% (K19, K20). | A letter is linked if either ID matches a live bundle. |
| Rhapsody submission-product links | 100% to submissions and products; bundle ID 99.5% (K11–K14, K17, K18). | |
| Rhapsody bulk products | 24.4M rows on only 10 STNs and 23 bundles (K16, K47); disjoint from the regular list (K15). | |
| PMS STN ↔ Rhapsody STN | 59.8% of PMS STNs are in Rhapsody (K30). PMS holds 15,767 GF and 1,675 AS STNs that Rhapsody mostly lacks (P2.32). | Expect only STN prefixes Rhapsody handles. |
| PMS details and code lists | Details record 100% (K31). Category, subcategory, flavor, package type and nicotine source all link by **code**, never by record number (K52–K59, P2.25). Subcategory code BIDI is not on the list (P2.26). | |
| Reporting | STNs 100% (K32), bundles 100% (K37), letters 100% (K60), facts → submissions 100% (K61). Submissions all come from Rhapsody (P2.29). eSUB-only STNs are not expected in Reporting (K33). eSUB links 98.4% found (K62). | |

## What the values mean

| Field | Values | Meaning used |
|---|---|---|
| `rhapsody_submissions.organization` | 3,679 company names (P2.17) | The **applicant company**, shown as "Organization". Not a CTP office. |
| `rhapsody_submissions.submission_status` | blank 75%, `1` 16%, `0` 8.5% (P2.14) | `1` open, `0` closed, blank not judged (assumed). |
| `rhapsody_submissions.stdz_submission_type` | SE, PMTA, Other, EX, MRTPA (P2.16) | Submission type filter values. |
| `rhapsody_bundle.bundlestatus` | 23 values (P2.11) | Finished: refuse to accept/file, withdrawn, granted, denied, cancelled, rescinded, administratively closed or withdrawn, removed from review. |
| `rhapsody_bundle.s_instance_status` | Complete 78%, In Progress 21%, New 0.8% (P2.13) | Complete = finished. |
| `rhapsody_notf_task_instance.state` | 5 Completed, 8 Obsolete, 2 Assigned, 1 Created (P2.19, P2.20) | Completed and Obsolete are done. No open task names an STN in its title (P4 U20, K48). |
| Letter types | File rates range from 0.3% (Jurisdiction Review) to 99.9% (Filing Letter) (P2.21) | Only expect a file for types that normally have one. |
| eSUB link types | 8, 12, 15 are almost never in Reporting (P2.10) | Not loaded by design. |
| eSUB `DATE_RECEIVED` | Stored at midnight; received dates match Rhapsody exactly for 100% of 43,906 STNs (P3.1, P3.2) | Compare received dates exactly. |
| STNs | No spaces, lowercase, dashes or dots in any system (P2.33) | |

## Uniqueness (P4)

Every ID checked is unique, except one placeholder value: one Rhapsody `product_id` sits on 14,359
rows and one PMS `uuid` on 14,368 products (U07, U10). PMS category codes appear twice each, once
active and once inactive (U12). Attribute codes are unique (U14). STN + PD number is unique (U15).

## Size

PMS holds 26.7M products, 24.4M of them bulk (P2.24). Rhapsody's bulk list has 24.4M rows. eSUB has
24.9M documents (K42).

## Not yet returned

P2.01–P2.06 (eSUB inactive, deleted and hold flags, status, document type) and P2.30.
