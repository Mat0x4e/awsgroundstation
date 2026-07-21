"""Parse the VITA-49 Context Packet from the Ground Station pcap.

The context packet has CIF=0x39A18000, meaning these fields are present:
- Bit 29: Bandwidth
- Bit 28: IF Reference Frequency
- Bit 27: RF Reference Frequency
- Bit 25: IF Band Offset (0x39 = bits 29,28,27,25,24 = BW, IFref, RFref, IFoffset, RefLevel)
- Bit 24: Reference Level
- Bit 21: Sample Rate
- Bit 15: Gain

Fields appear in order of CIF bit position (MSB first).
Each frequency/rate field is 8 bytes (64-bit VITA-49 fixed-point, radix 20).
Gain is 4 bytes. Reference level is 4 bytes (radix 7).
"""

import struct

# Context data from the pcap (56 bytes payload after the 28-byte VITA header):
CTX_HEX = "39a1800000001d164a000000000000000000000000033611380000000000f20000001080000020b913400000a00a0000a00001c700000000"
ctx_data = bytes.fromhex(CTX_HEX)


def vita_fixed_64(raw: int) -> float:
    """VITA-49 64-bit fixed-point with radix 20."""
    if raw >= (1 << 63):
        raw -= (1 << 64)
    return raw / float(1 << 20)


def vita_fixed_32_r7(raw: int) -> float:
    """VITA-49 32-bit fixed-point with radix 7 (for gain/level)."""
    # Only lower 16 bits, signed
    val = raw & 0xFFFF
    if val >= (1 << 15):
        val -= (1 << 16)
    return val / float(1 << 7)


def main():
    print(f"Context packet payload: {len(ctx_data)} bytes")
    print(f"Hex: {ctx_data.hex()}")
    
    # Word 0: CIF (Context Indicator Field)
    cif = struct.unpack("!I", ctx_data[0:4])[0]
    print(f"\nCIF: 0x{cif:08X} = {cif:032b}")
    
    # Decode CIF bits (bit 31 is MSB)
    fields_present = []
    CIF_FIELDS = {
        31: ("Change Indicator", 0),
        30: ("Reference Point ID", 4),
        29: ("Bandwidth", 8),
        28: ("IF Reference Frequency", 8),
        27: ("RF Reference Frequency", 8),
        26: ("RF Reference Frequency Offset", 8),
        25: ("IF Band Offset", 8),
        24: ("Reference Level", 4),
        23: ("Gain", 4),
        22: ("Over-range Count", 4),
        21: ("Sample Rate", 8),
        20: ("Timestamp Adjustment", 8),
        19: ("Timestamp Calibration Time", 4),
        18: ("Temperature", 4),
        17: ("Device ID", 8),
        16: ("State/Event Indicators", 4),
        15: ("Signal Data Packet Payload Format", 8),
    }
    
    offset = 4  # After CIF word
    results = {}
    
    for bit in range(31, 14, -1):
        if cif & (1 << bit):
            name, size = CIF_FIELDS.get(bit, (f"Unknown(bit {bit})", 4))
            if bit == 31:  # Change indicator — no data
                fields_present.append((bit, name, 0, None))
                continue
            field_data = ctx_data[offset:offset + size]
            fields_present.append((bit, name, size, field_data))
            offset += size
    
    print(f"\nFields present (in order):")
    for bit, name, size, field_data in fields_present:
        if field_data is None:
            print(f"  Bit {bit}: {name} (indicator only)")
            continue
            
        print(f"  Bit {bit}: {name} ({size} bytes) = {field_data.hex()}")
        
        if size == 8 and name in ("Bandwidth", "IF Reference Frequency", 
                                    "RF Reference Frequency", "IF Band Offset",
                                    "RF Reference Frequency Offset", "Sample Rate"):
            raw = struct.unpack("!q", field_data)[0]
            hz = vita_fixed_64(raw)
            mhz = hz / 1e6
            print(f"         → {hz:,.0f} Hz = {mhz:.6f} MHz")
            results[name] = {"hz": hz, "mhz": mhz}
            
        elif size == 4 and name == "Reference Level":
            raw = struct.unpack("!I", field_data)[0]
            dbm = vita_fixed_32_r7(raw)
            print(f"         → {dbm:.2f} dBm")
            results[name] = dbm
            
        elif size == 4 and name == "Gain":
            raw = struct.unpack("!I", field_data)[0]
            gain1 = vita_fixed_32_r7(raw >> 16)  # Stage 2 (back-end)
            gain2 = vita_fixed_32_r7(raw & 0xFFFF)  # Stage 1 (front-end)
            print(f"         → Stage 1: {gain2:.2f} dB, Stage 2: {gain1:.2f} dB")
            results[name] = {"stage1": gain2, "stage2": gain1}
            
        elif size == 8 and name == "Signal Data Packet Payload Format":
            raw = struct.unpack("!Q", field_data)[0]
            # Bits 63-59: Packing Method, Real/Complex, Data Item Format
            # Bits 58-54: Repeat Count
            # Bit 53: Event tag size
            # Bit 52: Channel tag size
            # Bits 51-48: Data item fraction size
            # Bits 47-32: Item packing field size
            # Bits 31-16: Data item size
            # Bits 15-0: Repeat count
            real_complex = (raw >> 61) & 0x3
            data_format = (raw >> 56) & 0x1F
            item_size = ((raw >> 32) & 0x3F) + 1
            repeat = (raw >> 16) & 0xFFFF
            vector_size = (raw & 0xFFFF) + 1
            
            RC_NAMES = {0: "Real", 1: "Complex Cartesian", 2: "Complex Polar"}
            FMT_NAMES = {0: "Signed Fixed-Point", 7: "Unsigned Fixed-Point", 
                        14: "IEEE 754 Float", 16: "Signed VRT"}
            
            print(f"         → Real/Complex: {RC_NAMES.get(real_complex, real_complex)}")
            print(f"         → Data format: {FMT_NAMES.get(data_format, data_format)}")
            print(f"         → Item size: {item_size} bits")
            print(f"         → Vector size: {vector_size}")
            results["Payload Format"] = {
                "real_complex": RC_NAMES.get(real_complex, str(real_complex)),
                "data_format": FMT_NAMES.get(data_format, str(data_format)),
                "item_size_bits": item_size,
                "vector_size": vector_size,
            }

    print(f"\nBytes consumed: {offset} / {len(ctx_data)}")
    print(f"Remaining: {ctx_data[offset:].hex()}")
    
    # Final summary
    print("\n" + "="*60)
    print("=== SIGNAL CHARACTERISTICS ===")
    print("="*60)
    if "Bandwidth" in results:
        print(f"  Bandwidth: {results['Bandwidth']['mhz']:.3f} MHz")
    if "RF Reference Frequency" in results:
        print(f"  RF Center: {results['RF Reference Frequency']['mhz']:.3f} MHz")
    if "IF Reference Frequency" in results:
        print(f"  IF Freq: {results['IF Reference Frequency']['mhz']:.3f} MHz")
    if "Sample Rate" in results:
        print(f"  Sample Rate: {results['Sample Rate']['mhz']:.3f} MSps")
    if "Payload Format" in results:
        pf = results["Payload Format"]
        print(f"  Data Type: {pf['real_complex']}, {pf['data_format']}")
        print(f"  Sample Width: {pf['item_size_bits']} bits per component")
    if "Gain" in results:
        print(f"  Gain: {results['Gain']}")
    if "Reference Level" in results:
        print(f"  Ref Level: {results['Reference Level']:.2f} dBm")


if __name__ == "__main__":
    main()
