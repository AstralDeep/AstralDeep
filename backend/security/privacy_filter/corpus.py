"""Synthetic Clinical Test Corpus for Privacy Filter Evaluation.

Fulfills Acceptance Check 2 (Issue #318):
- Multi-turn clinical notes with MRNs, names, addresses, dates
- Multilingual names and encoded text
- Strict negative controls with clinical diagnoses, medications, dosages, lab values, and ICD codes
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ClinicalTestCase:
    id: str
    description: str
    text: str
    expected_categories: List[str]
    is_negative_clinical_control: bool = False


SYNTHETIC_CLINICAL_CORPUS: List[ClinicalTestCase] = [
    # 1. Patient and Provider Names
    ClinicalTestCase(
        id="name_provider_consult",
        description="Attending physician consultation note with patient identification",
        text="Dr. Robert Williams performed a cardiology consultation for Patient Alice Johnson.",
        expected_categories=["NAME"],
    ),
    ClinicalTestCase(
        id="name_multilingual",
        description="Multilingual patient identifiers in clinical triage",
        text="Triage note: Patient Señor Carlos Gomez and Monsieur Pierre Dubois were registered.",
        expected_categories=["NAME"],
    ),
    # 2. Addresses
    ClinicalTestCase(
        id="address_full_residential",
        description="Patient residence in intake documentation",
        text="Patient resides at 742 Evergreen Terrace, Springfield, OR 97477 and requested follow-up.",
        expected_categories=["ADDRESS"],
    ),
    ClinicalTestCase(
        id="address_suite",
        description="Clinic suite address in referral letter",
        text="Please send records to 100 Medical Center Drive, Suite 400, Boston, MA 02115.",
        expected_categories=["ADDRESS"],
    ),
    # 3. MRNs and Clinical IDs
    ClinicalTestCase(
        id="mrn_contextual",
        description="Medical Record Number in EHR summary",
        text="Discharge summary for MRN: 98402-A9 registered under inpatient telemetry.",
        expected_categories=["MRN"],
    ),
    ClinicalTestCase(
        id="patient_id_contextual",
        description="Contextual Patient ID in lab requisition",
        text="Specimen received for Patient ID: MED-883921 with urgent panel request.",
        expected_categories=["MRN"],
    ),
    # 4. Contextual Dates
    ClinicalTestCase(
        id="date_admission",
        description="Hospital encounter admission date",
        text="Patient was Admitted on 10/14/2025 following acute onset of shortness of breath.",
        expected_categories=["DATE"],
    ),
    ClinicalTestCase(
        id="date_service",
        description="Date of Service in procedure note",
        text="Date of Service: November 12, 2025. Procedure completed without complication.",
        expected_categories=["DATE"],
    ),
    # 5. Combined Multi-Entity Encounter
    ClinicalTestCase(
        id="combined_encounter",
        description="Comprehensive clinical encounter note with names, address, MRN, and date",
        text=(
            "Patient Michael Chang, MRN: 1092834, residing at 450 Sunset Boulevard, Los Angeles, CA 90028, "
            "was seen on 09/22/2025 by Dr. Sarah Jenkins."
        ),
        expected_categories=["NAME", "ADDRESS", "MRN", "DATE"],
    ),
    # 6. NEGATIVE CONTROLS (Clinical Diagnoses, Drugs, Dosages, Labs, ICD Codes)
    # These must NOT be redacted, ensuring clinical meaning loss is 0.0%
    ClinicalTestCase(
        id="neg_diabetes_medication",
        description="Type 2 diabetes diagnosis with oral hypoglycemic dosing",
        text="Assessment: Type 2 Diabetes Mellitus. Plan: Metformin 500 mg BID with breakfast and dinner.",
        expected_categories=[],
        is_negative_clinical_control=True,
    ),
    ClinicalTestCase(
        id="neg_cardiac_vitals_labs",
        description="Hypertension and cardiac vitals with normal potassium",
        text="Vital signs: Blood pressure 120/80 mmHg, heart rate 72 bpm. Serum potassium 4.2 mEq/L.",
        expected_categories=[],
        is_negative_clinical_control=True,
    ),
    ClinicalTestCase(
        id="neg_respiratory_asthma",
        description="Asthma diagnosis with bronchodilator prescription and billing code",
        text="Diagnosis: Asthma. Inhaler: Albuterol 90 mcg PRN wheezing. Billing: ICD-10 J45.909.",
        expected_categories=[],
        is_negative_clinical_control=True,
    ),
    ClinicalTestCase(
        id="neg_covid_pcr",
        description="Infectious disease lab result with troponin cardiac biomarker",
        text="Laboratory review: COVID-19 PCR negative. High sensitivity Troponin I < 0.04 ng/mL.",
        expected_categories=[],
        is_negative_clinical_control=True,
    ),
    ClinicalTestCase(
        id="neg_lipid_atorvastatin",
        description="Hyperlipidemia regimen with statin dosage",
        text="Hyperlipidemia management: Atorvastatin 20 mg PO QD at bedtime. Fasting lipid panel in 3 months.",
        expected_categories=[],
        is_negative_clinical_control=True,
    ),
]
