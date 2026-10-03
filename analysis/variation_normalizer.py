def normalize_variation(raw):
    device = ""
    laterality = "bilateral"

    if not raw:
        return device, laterality

    text = raw.lower().strip()

    # --- DEVICE ---
    if "smith" in text or "multi" in text:
        device = "Smith"
    elif "sz" in text or "seilzug" in text:
        device = "SZ"
    elif "kh" in text:
        device = "KH"
    elif "lh" in text or "langhantel" in text:
        device = "LH"
    elif "egym" in text:
        device = "eGym"
    elif "gym80" in text or "maschine" in text:
        device = "Maschine"
    elif "bw" in text or "bodyweight" in text or "weighted" in text:
        device = "BW"

    # --- LATERALITY ---
    if "uni" in text or "unilat" in text:
        laterality = "unilateral"

    return device, laterality
