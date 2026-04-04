# Batch-Aware OCR Preprocessing & Inference Pipeline  
### Iterative Implementation Plan (LLM-Guided, Python-Centric)

## 1. Project Goal

Build a two-pass, batch-aware OCR preprocessing pipeline for degraded historical newspapers that:
1. Extracts high-confidence text signals across a batch of chronologically related issues  
2. Learns typeface, layout, and degradation patterns  
3. Uses that information to recover low-confidence regions  
4. Produces OCR-optimized outputs  
5. Iterates with human-in-the-loop validation  

## 2. Core Design Principles

1. Iterative development only  
2. Precision-first approach  
3. Batch-aware learning  
4. Separation of concerns  
5. Artifact-first design  
6. Use existing tools when possible  

## 3. Recommended Toolchain

### Core Python Libraries
- opencv-python  
- numpy  
- scikit-image  
- scipy  
- pandas  
- networkx  
- faiss or sklearn.neighbors  
- pydantic  
- matplotlib / seaborn  

### OCR Tools
- Tesseract OCR  
- OCRmyPDF  

### Optional Tools
- torch / tensorflow  
- kraken / calamari  
- imgaug  
- ImageMagick  

## 4. Project Structure

project_root/
├── ocr_pipeline/
│   ├── config.py
│   ├── types.py
│   ├── logging_utils.py
│   ├── artifacts.py
│   ├── stages/
│   └── batch_runner.py
├── artifacts/
└── existing_program/

---

# PHASED IMPLEMENTATION (ITERATIVE TODO)

## PHASE 0 — Integration Mapping
- Identify image entry points  
- Identify OCR locations  
- Define insertion points  

STOP → USER VALIDATION  

## PHASE 1 — Scaffolding
- Create module structure  
- Add logging  
- Add artifact directories  

STOP → USER VALIDATION  

## PHASE 2 — Image Ingestion
- Load grayscale  
- Capture metadata  
- Save artifacts  

STOP → USER VALIDATION  

## PHASE 3 — High-Confidence Sweep
- Illumination flattening  
- CLAHE  
- Conservative threshold  
- Connected components  

STOP → USER VALIDATION  

## PHASE 4 — Feature Extraction
- Character size  
- Stroke width  
- Layout  

STOP → USER VALIDATION  

## PHASE 5 — Feature Store
- Store glyphs  
- Store descriptors  
- Enable lookup  

STOP → USER VALIDATION  

## PHASE 6 — OCR Probe
- Run OCR on strong regions  
- Store text + confidence  

STOP → USER VALIDATION  

## PHASE 7 — Low-Confidence Detection
- Identify faint regions  
- Produce masks  

STOP → USER VALIDATION  

## PHASE 8 — Retrieval Inference
- Find similar glyphs  
- Use batch memory  

STOP → USER VALIDATION  

## PHASE 9 — Temporal Weighting
- Weight nearby issues higher  

STOP → USER VALIDATION  

## PHASE 10 — Second-Pass Refinement
- Combine signals  
- Produce refined output  

STOP → USER VALIDATION  

## PHASE 11 — Adaptive Learning
- Train lightweight models  
- Use only high-confidence data  

STOP → USER VALIDATION  

## PHASE 12 — OCR Comparison
- Compare multiple outputs  
- Select best  

STOP → USER VALIDATION  

## PHASE 13 — Reporting
- Generate overlays  
- Provide review outputs  

STOP → USER VALIDATION  

## PHASE 14 — Configuration
- Expose key parameters  

STOP → USER VALIDATION  

## PHASE 15 — Regression Testing
- Freeze validation set  
- Compare results  

STOP → USER VALIDATION  

## PHASE 16 — Batch Runner
- Integrate full pipeline  

STOP → USER VALIDATION  

---

## Human-in-the-Loop Feedback Template

Step completed:  
Did it run:  
Errors:  
Artifacts generated:  
What looks right:  
What looks wrong:  
Priority fix:  

---

## LLM Control Prompt

Work in small steps.  
Do not implement the full system at once.  
Add one feature at a time with logging and artifacts.  
Stop after each step and wait for validation.  
Preserve existing behavior.  
Prioritize high-confidence precision.  
Do not trust low-confidence data as ground truth.  
