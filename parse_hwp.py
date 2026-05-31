#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pyelftools",
# ]
# ///
"""
parse_hwp.py - Parse RTK hardware profiles from rtcore.ko and output C source.

hwp_swDescp_t layout (confirmed offsets):
  [0-3]:       chip_id (uint32 BE)
  [4]:         swcore_supported (uint8)
  [5-7]:       padding (3 bytes, before 4-byte enum)
  [8-11]:      swcore_access_method (hwp_swRegAccMethod_t, 4-byte enum)
  [12]:        swcore_spi_chip_select (uint8)
  [13]:        nic_supported (uint8)
  [14-15]:     padding (2 bytes, before 4-byte-aligned port struct)
  [16]:        port.count (uint8, always 0 in static data)
  [17-19]:     padding (3 bytes, to align hwp_portDescp_t to 4 bytes)
  [20..1299]:  port.descp[64], each 20 bytes
  [1300]:      serdes.count (uint8)
  [1301..1348]:serdes.descp[24], each 2 bytes
  [1349-1351]: padding (3 bytes, to align hwp_scDescp_t to 4 bytes)
  [1352]:      sc.count (uint8)
  [1353-1355]: padding (3 bytes)
  [1356..1419]:sc.descp[8], each 8 bytes
  [1420]:      phy.count (uint8)
  [1421-1423]: padding (3 bytes)
  [1424..1551]:phy.descp[16], each 8 bytes
  [1552-1555]: led.descp.led_if_sel (uint32 BE)
  [1556-1559]: led.descp.led_active (uint32 BE)
  [1560..1639]:led.descp.led_definition_set[4][5], each 4 bytes BE
"""

import sys
import struct
from elftools.elf.elffile import ELFFile

# ============================================================
#  Constants
# ============================================================

HWP_NONE   = 0xFF
HWP_END    = 0xFF
HWP_LED_END = 0xFFFFFFFF
SDS_MASK_BIT = 31  # bit 31 set => sds_idx is a bitmap

# ============================================================
#  Enum name maps
# ============================================================

ACC_METHOD = {
    0: "HWP_SW_ACC_NONE",
    1: "HWP_SW_ACC_MEM",
    2: "HWP_SW_ACC_SPI",
    3: "HWP_SW_ACC_PCIe",
    4: "HWP_SW_ACC_I2C",
    5: "HWP_SW_ACC_VIR",
}

SERDES_MODE = {
    0:  "RTK_MII_NONE",
    1:  "RTK_MII_DISABLE",
    2:  "RTK_MII_10GR",
    3:  "RTK_MII_RXAUI",
    4:  "RTK_MII_RXAUI_LITE",
    5:  "RTK_MII_RXAUISGMII_AUTO",
    6:  "RTK_MII_RXAUI1000BX_AUTO",
    7:  "RTK_MII_RSGMII_PLUS",
    8:  "RTK_MII_SGMII",
    9:  "RTK_MII_QSGMII",
    10: "RTK_MII_1000BX_FIBER",
    11: "RTK_MII_100BX_FIBER",
    12: "RTK_MII_1000BX100BX_AUTO",
    13: "RTK_MII_10GR1000BX_AUTO",
    14: "RTK_MII_10GRSGMII_AUTO",
    15: "RTK_MII_XAUI",
    16: "RTK_MII_RMII",
    17: "RTK_MII_SMII",
    18: "RTK_MII_SSSMII",
    19: "RTK_MII_RSGMII",
    20: "RTK_MII_XSMII",    # = RTK_MII_RS8MII (obsoleted)
    21: "RTK_MII_XSGMII",
    22: "RTK_MII_QHSGMII",
    23: "RTK_MII_HISGMII",
    24: "RTK_MII_HISGMII_5G",
    25: "RTK_MII_DUAL_HISGMII",
    26: "RTK_MII_2500Base_X",
    27: "RTK_MII_RXAUI_PLUS",
    28: "RTK_MII_USXGMII_10GSXGMII",
    29: "RTK_MII_USXGMII_10GDXGMII",
    30: "RTK_MII_USXGMII_10GQXGMII",
    31: "RTK_MII_USXGMII_5GSXGMII",
    32: "RTK_MII_USXGMII_5GDXGMII",
    33: "RTK_MII_USXGMII_2_5GSXGMII",
    34: "RTK_MII_USXGMII_1G",
    35: "RTK_MII_USXGMII_100M",
    36: "RTK_MII_USXGMII_10M",
    37: "RTK_MII_5GBASEX",
    38: "RTK_MII_5GR",
    39: "RTK_MII_XFI_5G_ADAPT",
    40: "RTK_MII_XFI_5G_CPRI",
    41: "RTK_MII_XFI_2P5G_ADAPT",
    42: "RTK_MII_QUSGMII",
    43: "RTK_MII_OUSGMII",
}

PHY_TYPE = {
    0:  "RTK_PHYTYPE_NONE",
    1:  "RTK_PHYTYPE_RTL8208D",
    2:  "RTK_PHYTYPE_RTL8208G",
    3:  "RTK_PHYTYPE_RTL8208L",
    4:  "RTK_PHYTYPE_RTL8208L_INT",
    5:  "RTK_PHYTYPE_RTL8212B",
    6:  "RTK_PHYTYPE_RTL8214FB",
    7:  "RTK_PHYTYPE_RTL8214B",
    8:  "RTK_PHYTYPE_RTL8214FC",
    9:  "RTK_PHYTYPE_RTL8214C",
    10: "RTK_PHYTYPE_RTL8218B",
    11: "RTK_PHYTYPE_RTL8218FB",
    12: "RTK_PHYTYPE_RTL8218D",
    13: "RTK_PHYTYPE_RTL8295R",
    14: "RTK_PHYTYPE_RTL8214QF",
    15: "RTK_PHYTYPE_RTL8224QF",
    16: "RTK_PHYTYPE_RTL8218D_NMP",
    17: "RTK_PHYTYPE_RTL8295R_C22",
    18: "RTK_PHYTYPE_RTL8214QF_NC5",
    19: "RTK_PHYTYPE_RTL8226",
    20: "RTK_PHYTYPE_RTL8226B",
    21: "RTK_PHYTYPE_RTL8218E",
    22: "RTK_PHYTYPE_RTL8261",
    23: "RTK_PHYTYPE_RTL8264",
    24: "RTK_PHYTYPE_RTL8261I",
    25: "RTK_PHYTYPE_RTL8264I",
    26: "RTK_PHYTYPE_RTL8251",
    27: "RTK_PHYTYPE_RTL8254",
    28: "RTK_PHYTYPE_RTL8251I",
    29: "RTK_PHYTYPE_RTL8254I",
    30: "RTK_PHYTYPE_RTL8251L",
    31: "RTK_PHYTYPE_RTL8254L",
    32: "RTK_PHYTYPE_RTL8224",
    33: "RTK_PHYTYPE_RTL8261B",
    34: "RTK_PHYTYPE_RTL8264B",
    35: "RTK_PHYTYPE_SERDES",
    36: "RTK_PHYTYPE_CUST1",
    37: "RTK_PHYTYPE_CUST2",
    38: "RTK_PHYTYPE_CUST3",
    39: "RTK_PHYTYPE_CUST4",
    40: "RTK_PHYTYPE_CUST5",
    41: "RTK_PHYTYPE_EXP_RTL8211FS",
    42: "RTK_PHYTYPE_UNKNOWN",
    43: "RTK_PHYTYPE_INVALID",
}

LED_IF_SEL = {
    0: "LED_IF_SEL_NONE",
    1: "LED_IF_SEL_SERIAL",
    2: "LED_IF_SEL_SINGLE_COLOR_SCAN",
    3: "LED_IF_SEL_BI_COLOR_SCAN",
}

LED_ACTIVE = {
    0: "LED_ACTIVE_HIGH",
    1: "LED_ACTIVE_LOW",
}

# Well-known profile IDs (hwp_id_e enum values)
PROFILE_ID = {
    9300001: "HWP_RTL9301_2x8214QF_4XGE",
    9300002: "HWP_RTL9301_3x8218B_4XGE",
    9300003: "HWP_RTL9301_8218B_4XGE",
    9300004: "HWP_RTL9301_8218B_4XGE_CASCADE",
    9300005: "HWP_RTL9301_2x8214QF_4XGE_CASCADE",
    9300006: "HWP_RTL9301_14QF_4XGE_18B_4XGE_CASCADE",
    9300007: "HWP_RTL9301_3x8218D_4XGE",
    9300008: "HWP_RTL9301_6x8214QF_4XGE",
    9300009: "HWP_RTL9301_2x8214FC_4x8214QF_4XGE",
    9300010: "HWP_RTL9301_6x8218D_2x8295R_CASCADE",
    9300011: "HWP_RTL9301_2x8218B_4x8218D_2x8295R_CASCADE",
    9300012: "HWP_RTL9302B_2x8218D_2xCUST1_4XGE",
    9300013: "HWP_RTL9302C_4xCUST1",
    9300014: "HWP_RTL9303_2xCUST1",
    9300015: "HWP_RTL9303_8XGE",
    9300016: "HWP_RTL9302DE_2XRTL8284",
    9300017: "HWP_RTL9302D_6x8224QF_2XGE",
    9300018: "HWP_RTL9302C_2xRTL8284_2XGE",
    9300019: "HWP_RTL9302C_2xRTL8224_2XGE",
    9300020: "HWP_RTL9302C_2xRTL8224_2XGE_2xRTL8261N",
    9300021: "HWP_RTL9302C_4xRTL8284_4XGE",
    9300022: "HWP_RTL9302B_2x8218D_2x8284_4XGE",
    9300023: "HWP_RTL9302B_2x8218E_2x8224QF_4XGE",
    9300024: "HWP_RTL9303_8x8226",
    9300025: "HWP_RTL9301_3x8218D_2x8226CARD_2XGE",
    9300026: "HWP_RTL9303_8x2_5G",
    9300027: "HWP_RTL9301_3x8218E_4XGE",
    9300028: "HWP_RTL9303_2x8254L_DEMO",
    9300029: "HWP_RTL9303_6x8254L_6xSPI",
}

# chip_id (from include/hal/chipdef/chip.h)
CHIP_ID_MAP = {
    0x93010000: "RTL9301_CHIP_ID",
    0x93016810: "RTL9301_CHIP_ID_24G",
    0x93020000: "RTL9302A_CHIP_ID",
    0x93022000: "RTL9302D_CHIP_ID",
    0x93022400: "RTL9302DE_CHIP_ID",
    0x93022800: "RTL9302B_CHIP_ID",
    0x93022C00: "RTL9302C_CHIP_ID",
    0x93030000: "RTL9303_CHIP_ID",
    0x93100000: "RTL9310_CHIP_ID",
    0x93112000: "RTL9311_CHIP_ID",
    0x93112800: "RTL9311E_CHIP_ID",
    0x93112C00: "RTL9311R_CHIP_ID",
    0x93120000: "RTL9312_CHIP_ID",
    0x93130000: "RTL9313_CHIP_ID",
}

# ============================================================
#  Helper formatters
# ============================================================

def _vc(name: str, val: int) -> str:
    """Append ' /* 0xNN */' when name is a symbolic constant, not a raw numeric literal."""
    try:
        int(name, 0)
        return name  # already numeric
    except ValueError:
        return f"{name} /* 0x{val:x} */"

def fmt_chip_id(val):
    return _vc(CHIP_ID_MAP.get(val, f"0x{val:08X}"), val)

def fmt_byte_or_none(val):
    return _vc("HWP_NONE", val) if val == HWP_NONE else str(val)

def fmt_sds_idx(val):
    if val == 0xFFFFFFFF or (val & 0xFF) == HWP_NONE and val >> 8 == 0:
        return _vc("HWP_NONE", val)
    if val & (1 << SDS_MASK_BIT):
        # Bitmap format: bit 31 set, remaining bits = serdes mask
        bits = [i for i in range(SDS_MASK_BIT) if val & (1 << i)]
        if len(bits) == 1:
            return _vc(f"SBM({bits[0]})", val)
        return _vc(" | ".join(f"SBM({b})" for b in bits), val)
    return str(val)

def fmt_attr(val):
    if val == HWP_NONE:
        return _vc("HWP_NONE", val)
    if val == 0:
        return "0"
    bits = []
    if val & (1 << 0): bits.append("HWP_ETH")
    if val & (1 << 1): bits.append("HWP_UPLINK")
    if val & (1 << 2): bits.append("HWP_CASCADE")
    if val & (1 << 3): bits.append("HWP_CPU")
    if val & (1 << 4): bits.append("HWP_SC")
    remaining = val & ~0x1F
    if remaining:
        bits.append(f"0x{remaining:02x}")
    return _vc(" | ".join(bits) if bits else f"0x{val:02x}", val)

def fmt_eth(val):
    if val == HWP_NONE or val == 0:
        return _vc("HWP_NONE", val)
    bits = []
    if val & (1 << 0): bits.append("HWP_FE")
    if val & (1 << 1): bits.append("HWP_GE")
    if val & (1 << 2): bits.append("HWP_2_5GE")
    if val & (1 << 3): bits.append("HWP_5GE")
    if val & (1 << 4): bits.append("HWP_XGE")
    if val & (1 << 5): bits.append("HWP_SXGE")
    remaining = val & ~0x3F
    if remaining:
        bits.append(f"0x{remaining:02x}")
    return _vc(" | ".join(bits) if bits else f"0x{val:02x}", val)

def fmt_medi(val):
    if val == HWP_NONE or val == 0:
        return _vc("HWP_NONE", val)
    bits = []
    if val & (1 << 0): bits.append("HWP_COPPER")
    if val & (1 << 1): bits.append("HWP_FIBER")
    if val & (1 << 2): bits.append("HWP_COMBO")
    if val & (1 << 3): bits.append("HWP_SERDES")
    remaining = val & ~0x0F
    if remaining:
        bits.append(f"0x{remaining:02x}")
    return _vc(" | ".join(bits) if bits else f"0x{val:02x}", val)

def fmt_led_layout(val):
    if val == HWP_NONE:  return _vc("HWP_NONE", val)
    if val == 0:         return _vc("SINGLE_SET", val)
    if val == 1:         return _vc("DOUBLE_SET", val)
    return str(val)

def fmt_polarity(val):
    return _vc("SERDES_POLARITY_CHANGE" if val else "SERDES_POLARITY_NORMAL", val)

def fmt_led_val(val):
    return _vc("HWP_LED_END", val) if val == HWP_LED_END else f"0x{val:X}"

def fmt_phy_type(val):
    if val == HWP_NONE:
        return _vc("HWP_END", val)
    return _vc(PHY_TYPE.get(val, f"0x{val:08X}"), val)

def c_ident(name):
    """Convert a profile name string to a valid C identifier."""
    out = []
    for c in name.lower():
        if c.isalnum() or c == '_':
            out.append(c)
        elif c in (' ', '-', '+', '.'):
            out.append('_')
    return ''.join(out).strip('_')


# ============================================================
#  ELF loader with relocation support
# ============================================================

class ELFImage:
    """
    Loads a relocatable MIPS BE .ko ELF and applies R_MIPS_32 relocations.
    Virtual address scheme: section base = sh_offset.
    """

    def __init__(self, path):
        self._sections = {}   # name -> (base_addr, bytearray)
        self._symbols = {}    # name -> resolved_addr

        with open(path, 'rb') as f:
            elf = ELFFile(f)

            # Collect all data sections
            for sec in elf.iter_sections():
                sh_type = sec['sh_type']
                if sh_type == 'SHT_PROGBITS':
                    data = bytearray(sec.data())
                elif sh_type == 'SHT_NOBITS':
                    data = bytearray(sec['sh_size'])
                else:
                    continue
                self._sections[sec.name] = (sec['sh_offset'], data)

            # Collect symbol table
            for sec in elf.iter_sections():
                if sec['sh_type'] not in ('SHT_SYMTAB', 'SHT_DYNSYM'):
                    continue
                for sym in sec.iter_symbols():
                    if not sym.name:
                        continue
                    shndx = sym['st_shndx']
                    if shndx in ('SHN_UNDEF', 'SHN_ABS', 'SHN_COMMON'):
                        continue
                    ref_sec = elf.get_section(shndx)
                    resolved = ref_sec['sh_offset'] + sym['st_value']
                    self._symbols[sym.name] = resolved

            # Apply R_MIPS_32 relocations
            for sec in elf.iter_sections():
                if sec['sh_type'] != 'SHT_REL':
                    continue
                target_sec = elf.get_section(sec['sh_info'])
                target_name = target_sec.name
                if target_name not in self._sections:
                    continue
                target_base, target_data = self._sections[target_name]

                symtab = elf.get_section(sec['sh_link'])
                for reloc in sec.iter_relocations():
                    if reloc['r_info_type'] != 2:  # R_MIPS_32
                        continue
                    off = reloc['r_offset']
                    if off + 4 > len(target_data):
                        continue
                    sym = symtab.get_symbol(reloc['r_info_sym'])
                    shndx = sym['st_shndx']
                    if shndx in ('SHN_UNDEF', 'SHN_ABS', 'SHN_COMMON'):
                        continue
                    ref_sec = elf.get_section(shndx)
                    sym_base = ref_sec['sh_offset']
                    # Addend is stored in the data at r_offset (implicit-addend REL)
                    addend = struct.unpack_from('>I', target_data, off)[0]
                    struct.pack_into('>I', target_data, off, sym_base + addend)

    # ---- Address access ----

    def _find_section(self, addr):
        for name, (base, data) in self._sections.items():
            if base <= addr < base + len(data):
                return base, data
        raise ValueError(f"Address 0x{addr:08x} not mapped in any section")

    def read_bytes(self, addr, length):
        base, data = self._find_section(addr)
        off = addr - base
        if off + length > len(data):
            raise ValueError(f"Read 0x{addr:08x}+{length} overruns section")
        return bytes(data[off:off+length])

    def u32(self, addr):
        return struct.unpack('>I', self.read_bytes(addr, 4))[0]

    def u8(self, addr):
        return struct.unpack('B', self.read_bytes(addr, 1))[0]

    def cstr(self, addr, maxlen):
        raw = self.read_bytes(addr, maxlen)
        end = raw.find(b'\x00')
        return raw[:end].decode('ascii', errors='replace') if end >= 0 else raw.decode('ascii', errors='replace')

    def find_symbol(self, name):
        return self._symbols.get(name, None)


# ============================================================
#  Struct parsers
# ============================================================

def _parse_port(img, addr):
    """Parse hwp_portDescp_t (20 bytes) at addr."""
    d = img.read_bytes(addr, 20)
    return {
        'mac_id':           d[0],
        'phy_idx':          d[1],
        'smi':              d[2],
        'phy_addr':         d[3],
        'sds_idx':          struct.unpack_from('>I', d, 4)[0],
        'attr':             d[8],
        'eth':              d[9],
        'medi':             d[10],
        'sc_idx':           d[11],
        'led_c':            d[12],
        'led_f':            d[13],
        'led_layout':       d[14],
        # MIPS BE bitfield byte [15]: phy_mdi_pin_swap:1 is first declared = MSB (bit 7)
        'phy_mdi_pin_swap': (d[15] >> 7) & 1,
        'phy_mdi_pair_swap': d[16],
    }

def _parse_serdes(img, addr):
    """Parse hwp_serdesDescp_t (2 bytes) at addr."""
    d = img.read_bytes(addr, 2)
    packed = d[1]
    # MIPS BE MSB-first: mode:6 = bits[7:2], rx_polarity:1 = bit[1], tx_polarity:1 = bit[0]
    return {
        'sds_id':       d[0],
        'mode':         (packed >> 2) & 0x3F,
        'rx_polarity':  (packed >> 1) & 1,
        'tx_polarity':  packed & 1,
    }

def _parse_sc(img, addr):
    """Parse hwp_scDescp_t (8 bytes) at addr."""
    d = img.read_bytes(addr, 8)
    pol = d[6]
    # MIPS BE MSB-first: rx_polarity:1 first = bit 7, tx_polarity:1 = bit 6
    return {
        'chip':         struct.unpack_from('>I', d, 0)[0],
        'smi':          d[4],
        'phy_addr':     d[5],
        'rx_polarity':  (pol >> 7) & 1,
        'tx_polarity':  (pol >> 6) & 1,
    }

def _parse_phy(img, addr):
    """Parse hwp_phyDescp_t (8 bytes) at addr."""
    d = img.read_bytes(addr, 8)
    return {
        'chip':    struct.unpack_from('>I', d, 0)[0],
        'phy_max': d[4],
        'mac_id':  d[5],
    }

def parse_swDescp(img, addr):
    """Parse hwp_swDescp_t at addr using confirmed empirical offsets."""
    sd = {}
    sd['chip_id']               = img.u32(addr + 0)
    sd['swcore_supported']      = img.u8(addr + 4)
    # [5-7] padding before 4-byte enum
    sd['swcore_access_method']  = img.u32(addr + 8)
    sd['swcore_spi_chip_select'] = img.u8(addr + 12)
    sd['nic_supported']         = img.u8(addr + 13)
    # [14-15] padding before port struct (4-byte alignment)

    # Port array: count at [16], 3-byte pad, descp at [20]; mac_id=0xFF terminates
    ports = []
    for i in range(64):
        p = _parse_port(img, addr + 20 + i * 20)
        if p['mac_id'] == HWP_END:
            break
        ports.append(p)
    sd['ports'] = ports

    # Serdes array: count at [1300], descp at [1301]; sds_id=0xFF terminates
    serdes = []
    for i in range(24):
        s = _parse_serdes(img, addr + 1301 + i * 2)
        if s['sds_id'] == HWP_END:
            break
        serdes.append(s)
    sd['serdes'] = serdes

    # SC array: count at [1352], 3-byte pad, descp at [1356]; chip=0xFF or chip=0 terminates
    sc_list = []
    for i in range(8):
        c = _parse_sc(img, addr + 1356 + i * 8)
        if c['chip'] == HWP_END or c['chip'] == 0:
            break
        sc_list.append(c)
    sd['sc'] = sc_list

    # PHY array: count at [1420], 3-byte pad, descp at [1424]; chip=0xFF or chip=0 terminates
    phys = []
    for i in range(16):
        p = _parse_phy(img, addr + 1424 + i * 8)
        if p['chip'] == HWP_END or p['chip'] == 0:
            break
        phys.append(p)
    sd['phys'] = phys

    # LED: led_if_sel at [1552], led_active at [1556], led_definition_set[4][5] at [1560]
    sd['led_if_sel'] = img.u32(addr + 1552)
    sd['led_active'] = img.u32(addr + 1556)
    led_sets = []
    for si in range(4):
        leds = []
        for li in range(5):
            val = img.u32(addr + 1560 + si * 20 + li * 4)
            leds.append(val)
            if val == HWP_LED_END:
                break
        led_sets.append(leds)
    sd['led_sets'] = led_sets

    return sd

def parse_hwProfile(img, addr):
    """Parse hwp_hwProfile_t (88 bytes) at addr."""
    prof = {}
    # hwp_identifier_t: type(4) + name[44] + id(4) = 52 bytes
    prof['id_type'] = img.u32(addr + 0)
    prof['id_name'] = img.cstr(addr + 4, 44)
    prof['id_id']   = img.u32(addr + 48)
    # hwp_socDescp_t: swDescp_index(1) + slaveInterruptPin(1) = 2 bytes at [52..53]
    prof['soc_swDescp_index']     = img.u8(addr + 52)
    prof['soc_slaveInterruptPin'] = img.u8(addr + 53)
    # [54-55] padding; sw_count at [56]
    prof['sw_count'] = img.u32(addr + 56)
    # swDescp pointers at [60..83], parsed_info at [84]
    prof['swDescp_ptrs'] = [img.u32(addr + 60 + i * 4) for i in range(6)]

    prof['swDescp'] = []
    for i in range(prof['sw_count']):
        ptr = prof['swDescp_ptrs'][i]
        if ptr != 0:
            prof['swDescp'].append(parse_swDescp(img, ptr))

    return prof


# ============================================================
#  C source emitter
# ============================================================

def emit_swDescp(sd, var_name, unit_idx=0, multi_unit=False):
    lines = []
    suffix = f"_unit{unit_idx}" if multi_unit else ""
    lines.append(f"static hwp_swDescp_t {var_name}{suffix}_swDescp = {{")
    lines.append("")
    lines.append(f"    .chip_id                    = {fmt_chip_id(sd['chip_id'])},")
    lines.append(f"    .swcore_supported           = {'TRUE' if sd['swcore_supported'] else 'FALSE'},")
    lines.append(f"    .swcore_access_method       = {_vc(ACC_METHOD.get(sd['swcore_access_method'], str(sd['swcore_access_method'])), sd['swcore_access_method'])},")
    spi = sd['swcore_spi_chip_select']
    lines.append(f"    .swcore_spi_chip_select     = {_vc('HWP_NOT_USED' if spi == HWP_NONE else str(spi), spi)},")
    lines.append(f"    .nic_supported              = {'TRUE' if sd['nic_supported'] else 'FALSE'},")
    lines.append("")

    # --- Port ---
    if sd['ports']:
        lines.append("    .port.descp = {")
        for p in sd['ports']:
            f1 = f".mac_id = {p['mac_id']:2d}"
            f2 = f".attr = {fmt_attr(p['attr'])}"
            f3 = f".eth = {fmt_eth(p['eth'])}"
            f4 = f".medi = {fmt_medi(p['medi'])}"
            f5 = f".sds_idx = {fmt_sds_idx(p['sds_idx'])}"
            f6 = f".phy_idx = {fmt_byte_or_none(p['phy_idx'])}"
            f7 = f".smi = {fmt_byte_or_none(p['smi'])}"
            f8 = f".phy_addr = {fmt_byte_or_none(p['phy_addr'])}"
            f9 = f".led_c = {fmt_byte_or_none(p['led_c'])}"
            f10 = f".led_f = {fmt_byte_or_none(p['led_f'])}"
            f11 = f".led_layout = {fmt_led_layout(p['led_layout'])}"
            f12 = f".phy_mdi_pin_swap = {p['phy_mdi_pin_swap']}"
            f13 = f".phy_mdi_pair_swap = {p['phy_mdi_pair_swap']}"
            line = "        { " + ", ".join([f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13]) + ", },"
            lines.append(line)
        lines.append("        { .mac_id = HWP_END },")
        lines.append("    },  /* port.descp */")
        lines.append("")

    # --- LED ---
    lines.append("    .led.descp = {")
    lines.append(f"        .led_active = {_vc(LED_ACTIVE.get(sd['led_active'], str(sd['led_active'])), sd['led_active'])},")
    lines.append(f"        .led_if_sel = {_vc(LED_IF_SEL.get(sd['led_if_sel'], str(sd['led_if_sel'])), sd['led_if_sel'])},")
    for si, leds in enumerate(sd['led_sets']):
        if not leds or leds[0] == HWP_LED_END:
            continue
        if all(v == 0 for v in leds):
            continue
        for li, val in enumerate(leds):
            lines.append(f"        .led_definition_set[{si}].led[{li}] = {fmt_led_val(val)},")
    lines.append("    },  /* led.descp */")
    lines.append("")

    # --- Serdes ---
    if sd['serdes']:
        lines.append("    .serdes.descp = {")
        for i, s in enumerate(sd['serdes']):
            mode = _vc(SERDES_MODE.get(s['mode'], str(s['mode'])), s['mode'])
            rx   = fmt_polarity(s['rx_polarity'])
            tx   = fmt_polarity(s['tx_polarity'])
            lines.append(f"        [{i}] = {{ .sds_id = {s['sds_id']}, .mode = {mode}, .rx_polarity = {rx}, .tx_polarity = {tx} }},")
        lines.append(f"        [{len(sd['serdes'])}] = {{ .sds_id = HWP_END }},")
        lines.append("    },  /* serdes.descp */")
        lines.append("")

    # --- SC ---
    if sd['sc']:
        lines.append("    .sc.descp = {")
        for i, c in enumerate(sd['sc']):
            rx = fmt_polarity(c['rx_polarity'])
            tx = fmt_polarity(c['tx_polarity'])
            lines.append(f"        [{i}] = {{ .chip = 0x{c['chip']:08X}, .smi = {fmt_byte_or_none(c['smi'])}, .phy_addr = {fmt_byte_or_none(c['phy_addr'])}, .rx_polarity = {rx}, .tx_polarity = {tx} }},")
        lines.append(f"        [{len(sd['sc'])}] = {{ .chip = HWP_END }},")
        lines.append("    },  /* sc.descp */")
        lines.append("")

    # --- PHY ---
    if sd['phys']:
        lines.append("    .phy.descp = {")
        for i, ph in enumerate(sd['phys']):
            lines.append(f"        [{i}] = {{ .chip = {fmt_phy_type(ph['chip'])}, .mac_id = {ph['mac_id']}, .phy_max = {ph['phy_max']} }},")
        lines.append(f"        [{len(sd['phys'])}] = {{ .chip = HWP_END }},")
        lines.append("    },  /* .phy.descp */")
        lines.append("")

    lines.append("};")
    lines.append("")
    return "\n".join(lines)

def emit_hwProfile(prof, var_name):
    lines = []
    lines.append(f"static hwp_hwProfile_t {var_name} = {{")
    lines.append("")
    lines.append(f"    .identifier.name        = \"{prof['id_name']}\",")
    id_val = prof['id_id']
    lines.append(f"    .identifier.id          = {_vc(PROFILE_ID.get(id_val, str(id_val)), id_val)},")
    lines.append("")
    lines.append(f"    .soc.swDescp_index      = {prof['soc_swDescp_index']},")
    lines.append(f"    .soc.slaveInterruptPin  = {fmt_byte_or_none(prof['soc_slaveInterruptPin'])},")
    lines.append("")
    lines.append(f"    .sw_count               = {prof['sw_count']},")
    lines.append("    .swDescp = {")
    multi = len(prof['swDescp']) > 1
    for i in range(prof['sw_count']):
        suffix = f"_unit{i}" if multi else ""
        lines.append(f"        [{i}]                 = &{var_name}{suffix}_swDescp,")
    lines.append("    }")
    lines.append("")
    lines.append("};")
    lines.append("")
    return "\n".join(lines)


# ============================================================
#  Main
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <rtcore.ko> [profile_name_filter]", file=sys.stderr)
        sys.exit(1)

    ko_path = sys.argv[1]
    name_filter = sys.argv[2].lower() if len(sys.argv) > 2 else None

    print(f"# Loading {ko_path} ...", file=sys.stderr)
    img = ELFImage(ko_path)

    # Locate hwp_hwProfile_list
    list_addr = img.find_symbol('hwp_hwProfile_list')
    if list_addr is None:
        print("ERROR: symbol 'hwp_hwProfile_list' not found in ELF symbol table", file=sys.stderr)
        sys.exit(1)
    print(f"# hwp_hwProfile_list at 0x{list_addr:x}", file=sys.stderr)

    # Read NULL-terminated pointer array
    profile_ptrs = []
    i = 0
    while True:
        ptr = img.u32(list_addr + i * 4)
        if ptr == 0:
            break
        profile_ptrs.append(ptr)
        i += 1
    print(f"# Found {len(profile_ptrs)} profiles", file=sys.stderr)

    # Header
    print("/*")
    print(" * Reconstructed RTK hardware profiles from rtcore.ko")
    print(" * Generated by parse_hwp.py")
    print(" */")
    print()
    print('#include "hw_profile.h"')
    print()

    # Parse and emit each profile
    var_names = []
    for prof_addr in profile_ptrs:
        try:
            prof = parse_hwProfile(img, prof_addr)
        except Exception as e:
            print(f"/* ERROR parsing profile at 0x{prof_addr:x}: {e} */", file=sys.stderr)
            var_names.append(None)
            continue

        if name_filter and name_filter not in prof['id_name'].lower():
            var_names.append(None)
            continue

        var_name = c_ident(prof['id_name']) or f"profile_{prof_addr:08x}"
        var_names.append(var_name)

        multi = len(prof['swDescp']) > 1
        for i, sd in enumerate(prof['swDescp']):
            print(emit_swDescp(sd, var_name, unit_idx=i, multi_unit=multi))

        print(emit_hwProfile(prof, var_name))

    # Profile list (only if no filter)
    if name_filter is None:
        print("hwp_hwProfile_t *hwp_hwProfile_list[] = {")
        for i, vn in enumerate(var_names):
            if vn:
                print(f"    &{vn},")
            else:
                print(f"    /* 0x{profile_ptrs[i]:x} skipped/error */")
        print("    NULL,")
        print("};")

if __name__ == '__main__':
    main()
