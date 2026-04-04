"""
ocr_pipeline.config — Configuration and constants for batch pipeline.

Reads collection.json and provides derived configuration for the
batch-aware preprocessing stages. Does not duplicate unt_ocr_correct.py
constants — imports them when needed.
"""

import json
from pathlib import Path
from dataclasses import dataclass, asdict


# Confidence threshold — mirrors TESS_CONF_MIN in unt_ocr_correct.py.
# Words below this are always disputed regardless of agreement.
CONF_THRESHOLD = 40

# High-confidence gate: for pseudo-labeling / batch learning,
# we require HIGHER confidence than the dispute threshold.
# This is the conservative pseudo-label gate from the theory doc.
HC_GATE_CONFIDENCE = 70

# Connected component size bounds (pixels) for character-sized objects.
# Used in feature extraction to filter noise and non-text.
CC_MIN_AREA = 20          # below this = speckle noise
CC_MAX_AREA = 10000       # above this = image artifact or border

# Stroke width estimation: Otsu threshold on distance transform
# of binarized character mask.
STROKE_WIDTH_BINS = 50


@dataclass
class PipelineConfig:
    """
    Configuration for the batch-aware pipeline, derived from collection.json
    plus pipeline-specific settings.
    """
    # From collection.json
    collection_dir: str = ""
    title_name: str = ""
    language: str = "German"
    typeface: str = "Fraktur"
    source_medium: str = "35mm microfilm"
    layout_type: str = "newspaper"
    expected_cols: int = 5

    # Pipeline behavior
    artifact_dir: str = ""        # where to store pipeline artifacts
    conf_threshold: int = CONF_THRESHOLD
    hc_gate: int = HC_GATE_CONFIDENCE
    save_debug_images: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_collection_json(cls, config_path: Path) -> "PipelineConfig":
        """Load from an existing collection.json file."""
        with open(config_path, encoding="utf-8") as f:
            cj = json.load(f)

        collection_dir = str(config_path.parent)
        artifact_dir = str(config_path.parent / "artifacts")

        return cls(
            collection_dir=collection_dir,
            title_name=cj.get("title_name", ""),
            language=cj.get("language", "German"),
            typeface=cj.get("typeface", "Fraktur"),
            source_medium=cj.get("source_medium", "35mm microfilm"),
            layout_type=cj.get("layout_type", "newspaper"),
            expected_cols=int(cj.get("expected_cols", 5)),
            artifact_dir=artifact_dir,
        )
