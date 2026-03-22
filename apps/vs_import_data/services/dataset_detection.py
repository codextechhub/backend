from __future__ import annotations

from ..models import DatasetTypeChoices


DATASET_HEADER_PATTERNS = {
    DatasetTypeChoices.STUDENTS: {
        "student_id",
        "admission_number",
        "student_name",
        "full_name",
        "class_name",
        "gender",
    },
    DatasetTypeChoices.STAFF: {
        "staff_id",
        "employee_id",
        "staff_name",
        "department",
        "role",
    },
    DatasetTypeChoices.CLASSES: {
        "class_name",
        "arm",
        "teacher",
        "level",
        "session",
    },
    DatasetTypeChoices.FEES: {
        "fee_name",
        "amount",
        "term",
        "session",
        "category",
    },
    DatasetTypeChoices.VENDORS: {
        "vendor_name",
        "company_name",
        "phone",
        "email",
        "service_type",
    },
}


def normalize_header(value: str) -> str:
    return str(value).strip().lower().replace(" ", "_")


def detect_dataset_type(headers: list[str]) -> dict:
    """
    Guess the dataset type based on uploaded headers.

    Returns:
        {
            "detected_dataset_type": "...",
            "confidence_score": 80.0,
            "matched_headers": [...]
        }
    """
    normalized_headers = {normalize_header(h) for h in headers}

    best_dataset_type = DatasetTypeChoices.GENERIC
    best_score = 0
    best_matches = []

    for dataset_type, expected_headers in DATASET_HEADER_PATTERNS.items():
        matched = normalized_headers.intersection(expected_headers)
        score = len(matched)

        if score > best_score:
            best_score = score
            best_dataset_type = dataset_type
            best_matches = list(matched)

    confidence_score = 0.0
    if best_dataset_type != DatasetTypeChoices.GENERIC:
        total_expected = len(DATASET_HEADER_PATTERNS[best_dataset_type])
        confidence_score = round((best_score / total_expected) * 100, 2)

    return {
        "detected_dataset_type": best_dataset_type,
        "confidence_score": confidence_score,
        "matched_headers": best_matches,
    }