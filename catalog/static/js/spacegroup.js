/**
 * spacegroup.js — Space group autocomplete and crystallographic site assignment UI.
 *
 * All 230 space groups are available for input via a text field with datalist
 * autocomplete. When a recognized space group is selected, site-assignment rows
 * show the actual Wyckoff positions for that space group. When the space group is
 * unrecognized or "Unknown", rows fall back to generic labels derived from the
 * structure family.
 *
 * Wyckoff data is provided for the ~18 space groups most commonly encountered in
 * high-entropy oxide (HEO) research. Any other SG number can still be typed freely;
 * the element-site dropdowns will show generic labels so the submission is never
 * blocked.
 */

// ---------------------------------------------------------------------------
// All 230 space groups — [number, HM-symbol]
// ---------------------------------------------------------------------------
export const ALL_SPACE_GROUPS = [
  // Triclinic
  [1,"P1"],[2,"P-1"],
  // Monoclinic
  [3,"P2"],[4,"P2₁"],[5,"C2"],[6,"Pm"],[7,"Pc"],
  [8,"Cm"],[9,"Cc"],[10,"P2/m"],[11,"P2₁/m"],
  [12,"C2/m"],[13,"P2/c"],[14,"P2₁/c"],[15,"C2/c"],
  // Orthorhombic
  [16,"P222"],[17,"P222₁"],[18,"P2₁2₁2"],[19,"P2₁2₁2₁"],
  [20,"C222₁"],[21,"C222"],[22,"F222"],[23,"I222"],[24,"I2₁2₁2₁"],
  [25,"Pmm2"],[26,"Pmc2₁"],[27,"Pcc2"],[28,"Pma2"],[29,"Pca2₁"],
  [30,"Pnc2"],[31,"Pmn2₁"],[32,"Pba2"],[33,"Pna2₁"],[34,"Pnn2"],
  [35,"Cmm2"],[36,"Cmc2₁"],[37,"Ccc2"],[38,"Amm2"],[39,"Aem2"],
  [40,"Ama2"],[41,"Aea2"],[42,"Fmm2"],[43,"Fdd2"],[44,"Imm2"],
  [45,"Iba2"],[46,"Ima2"],[47,"Pmmm"],[48,"Pnnn"],[49,"Pccm"],
  [50,"Pban"],[51,"Pmma"],[52,"Pnna"],[53,"Pmna"],[54,"Pcca"],
  [55,"Pbam"],[56,"Pccn"],[57,"Pbcm"],[58,"Pnnm"],[59,"Pmmn"],
  [60,"Pbcn"],[61,"Pbca"],[62,"Pnma"],[63,"Cmcm"],[64,"Cmce"],
  [65,"Cmmm"],[66,"Cccm"],[67,"Cmme"],[68,"Ccce"],[69,"Fmmm"],
  [70,"Fddd"],[71,"Immm"],[72,"Ibam"],[73,"Ibca"],[74,"Imma"],
  // Tetragonal
  [75,"P4"],[76,"P4₁"],[77,"P4₂"],[78,"P4₃"],[79,"I4"],[80,"I4₁"],
  [81,"P-4"],[82,"I-4"],[83,"P4/m"],[84,"P4₂/m"],[85,"P4/n"],
  [86,"P4₂/n"],[87,"I4/m"],[88,"I4₁/a"],[89,"P422"],[90,"P42₁2"],
  [91,"P4₁22"],[92,"P4₁2₁2"],[93,"P4₂22"],[94,"P4₂2₁2"],
  [95,"P4₃22"],[96,"P4₃2₁2"],[97,"I422"],[98,"I4₁22"],
  [99,"P4mm"],[100,"P4bm"],[101,"P4₂cm"],[102,"P4₂nm"],
  [103,"P4cc"],[104,"P4nc"],[105,"P4₂mc"],[106,"P4₂bc"],
  [107,"I4mm"],[108,"I4cm"],[109,"I4₁md"],[110,"I4₁cd"],
  [111,"P-42m"],[112,"P-42c"],[113,"P-42₁m"],[114,"P-42₁c"],
  [115,"P-4m2"],[116,"P-4c2"],[117,"P-4b2"],[118,"P-4n2"],
  [119,"I-4m2"],[120,"I-4c2"],[121,"I-42m"],[122,"I-42d"],
  [123,"P4/mmm"],[124,"P4/mcc"],[125,"P4/nbm"],[126,"P4/nnc"],
  [127,"P4/mbm"],[128,"P4/mnc"],[129,"P4/nmm"],[130,"P4/ncc"],
  [131,"P4₂/mmc"],[132,"P4₂/mcm"],[133,"P4₂/nbc"],[134,"P4₂/nnm"],
  [135,"P4₂/mbc"],[136,"P4₂/mnm"],[137,"P4₂/nmc"],[138,"P4₂/ncm"],
  [139,"I4/mmm"],[140,"I4/mcm"],[141,"I4₁/amd"],[142,"I4₁/acd"],
  // Trigonal
  [143,"P3"],[144,"P3₁"],[145,"P3₂"],[146,"R3"],[147,"P-3"],
  [148,"R-3"],[149,"P312"],[150,"P321"],[151,"P3₁12"],[152,"P3₁21"],
  [153,"P3₂12"],[154,"P3₂21"],[155,"R32"],[156,"P3m1"],[157,"P31m"],
  [158,"P3c1"],[159,"P31c"],[160,"R3m"],[161,"R3c"],[162,"P-31m"],
  [163,"P-31c"],[164,"P-3m1"],[165,"P-3c1"],[166,"R-3m"],[167,"R-3c"],
  // Hexagonal
  [168,"P6"],[169,"P6₁"],[170,"P6₅"],[171,"P6₂"],[172,"P6₄"],
  [173,"P6₃"],[174,"P-6"],[175,"P6/m"],[176,"P6₃/m"],
  [177,"P622"],[178,"P6₁22"],[179,"P6₅22"],[180,"P6₂22"],
  [181,"P6₄22"],[182,"P6₃22"],[183,"P6mm"],[184,"P6cc"],
  [185,"P6₃cm"],[186,"P6₃mc"],[187,"P-6m2"],[188,"P-6c2"],
  [189,"P-62m"],[190,"P-62c"],[191,"P6/mmm"],[192,"P6/mcc"],
  [193,"P6₃/mcm"],[194,"P6₃/mmc"],
  // Cubic
  [195,"P23"],[196,"F23"],[197,"I23"],[198,"P2₁3"],[199,"I2₁3"],
  [200,"Pm-3"],[201,"Pn-3"],[202,"Fm-3"],[203,"Fd-3"],[204,"Im-3"],
  [205,"Pa-3"],[206,"Ia-3"],[207,"P432"],[208,"P4₂32"],
  [209,"F432"],[210,"F4₁32"],[211,"I432"],[212,"P4₃32"],
  [213,"P4₁32"],[214,"I4₁32"],[215,"P-43m"],[216,"F-43m"],
  [217,"I-43m"],[218,"P-43n"],[219,"F-43c"],[220,"I-43d"],
  [221,"Pm-3m"],[222,"Pn-3n"],[223,"Pm-3n"],[224,"Pn-3m"],
  [225,"Fm-3m"],[226,"Fm-3c"],[227,"Fd-3m"],[228,"Fd-3c"],
  [229,"Im-3m"],[230,"Ia-3d"],
];

// Build fast lookup tables: number→HM and normalised-HM→number
const _NUM_TO_HM = {};
const _HM_TO_NUM = {};
ALL_SPACE_GROUPS.forEach(([n, hm]) => {
  _NUM_TO_HM[n] = hm;
  // normalise: strip subscripts, hyphens (except in -3 etc.), spaces, underscores
  const key = hm.replace(/[₀₁₂₃₄₅₆₇₈₉]/g, c => "012345678901234567890123456789"["₀₁₂₃₄₅₆₇₈₉".indexOf(c)])
                 .replace(/\s+/g, "")
                 .toLowerCase();
  _HM_TO_NUM[key] = n;
});

/**
 * Parse a space group number from a user-typed string.
 * Accepts: "225", "Fm-3m", "Fm-3m (#225)", "Fm3m", "F m -3 m", etc.
 * Returns an integer 1–230 or null if unrecognised / "unknown".
 */
export function parseSpacegroupNum(text) {
  if (!text) return null;
  const t = text.trim().toLowerCase();
  if (t === "unknown" || t === "") return null;

  // Try bare integer first
  const bare = t.match(/^\s*(\d{1,3})\s*$/);
  if (bare) {
    const n = parseInt(bare[1], 10);
    if (n >= 1 && n <= 230) return n;
  }

  // Try extracting a parenthesised number: "Fm-3m (#225)" → 225
  const paren = t.match(/\(#?\s*(\d{1,3})\s*\)/);
  if (paren) {
    const n = parseInt(paren[1], 10);
    if (n >= 1 && n <= 230) return n;
  }

  // Try loose number anywhere: "225 Fm-3m" or "sg 225"
  const loose = t.match(/\b(\d{1,3})\b/);
  if (loose) {
    const n = parseInt(loose[1], 10);
    if (n >= 1 && n <= 230) return n;
  }

  // Try normalised HM symbol lookup
  const key = t.replace(/[₀₁₂₃₄₅₆₇₈₉]/g, c => "012345678901234567890123456789"["₀₁₂₃₄₅₆₇₈₉".indexOf(c)])
               .replace(/\s+|_/g, "");
  if (_HM_TO_NUM[key] !== undefined) return _HM_TO_NUM[key];

  return null;
}

// ---------------------------------------------------------------------------
// Space-group suggestions per structure family (ordered by likelihood)
// ---------------------------------------------------------------------------
export const SG_SUGGESTIONS_BY_FAMILY = {
  rocksalt:   [225, 166, 229],
  spinel:     [227, 141],
  pyrochlore: [227, 225, 206],
  perovskite: [221, 167, 62, 127, 140, 141, 139, 194, 14, 15],
  fluorite:   [225, 227, 206, 205],
  other:      [225, 227, 221, 167, 62, 141, 136, 206, 194, 186, 166, 230, 205, 229],
  unknown:    [],
};

// ---------------------------------------------------------------------------
// Wyckoff positions (sites) per space group number
//
// Format: array of strings, first entry always "unknown".
// Each entry: "<mult><letter> — <common-name> (<coords> if helpful)"
//
// Coverage: the ~18 space groups most common in HEO research.
// For any SG not listed here, the UI falls back to structure-family labels.
// ---------------------------------------------------------------------------
export const WYCKOFF_SITES = {

  // -------------------------------------------------------------------
  // #225 Fm-3m  —  rocksalt (NaCl/MgO/NiO type) AND disordered fluorite
  // -------------------------------------------------------------------
  225: [
    "unknown",
    "4a — cation / M-site  (0, 0, 0)  [rocksalt cation, e.g. Mg, Co, Ni, Cu, Zn]",
    "4b — anion / X-site   (½, ½, ½)  [rocksalt anion, e.g. O in MgO]",
    "8c — anion            (¼, ¼, ¼)  [fluorite anion, e.g. O in CeO₂]",
    "24d — edge anion      (¼, 0, 0)",
    "48i — general position",
  ],

  // -------------------------------------------------------------------
  // #227 Fd-3m  —  spinel (AB₂O₄) AND pyrochlore (A₂B₂O₇)
  //
  // These share the space group but occupy different Wyckoff letters.
  // Spinel:     A on 8a (tet), B on 16d (oct), O on 32e
  // Pyrochlore: A on 16d (8-coord), B on 16c (6-coord oct), O on 48f, O' on 8b
  // Note: normal vs inverse spinel both use Fd-3m but swap 8a↔16d occupancy.
  // -------------------------------------------------------------------
  227: [
    "unknown",
    "8a  — A-site tetrahedral   (⅛,⅛,⅛)  [normal spinel A-cation; divalent e.g. Mg²⁺, Zn²⁺]",
    "16c — B-site octahedral    (0,0,0)    [pyrochlore B-cation; e.g. Zr⁴⁺, Ti⁴⁺, Sn⁴⁺]",
    "16d — B-site octahedral    (½,½,½)    [normal spinel B-cation; trivalent e.g. Fe³⁺, Al³⁺; also pyrochlore A-cation e.g. rare earths]",
    "32e — O anion              (x,x,x)    [spinel oxygen]",
    "48f — O anion              (x,⅛,⅛)   [pyrochlore 48f oxygen]",
    "8b  — O' anion             (⅜,⅜,⅜)   [pyrochlore O' oxygen; vacant in defect pyrochlore]",
    "8a  — vacant/interstitial  (⅛,⅛,⅛)   [vacant in ideal pyrochlore]",
  ],

  // -------------------------------------------------------------------
  // #221 Pm-3m  —  cubic perovskite ABO₃ (BaTiO₃ cubic, SrTiO₃)
  // Standard crystallographic setting: B at origin (1a), A at body-centre (1b)
  // -------------------------------------------------------------------
  221: [
    "unknown",
    "1a — B-site  (0, 0, 0)      [body-centre; higher-valence small cation e.g. Ti⁴⁺, Fe³⁺, Zr⁴⁺]",
    "1b — A-site  (½, ½, ½)      [corner; large lower-valence cation e.g. Ba²⁺, La³⁺, Ca²⁺]",
    "3c — O-site  (½, ½, 0)      [face-centre oxygen]",
    "3d — O-site  (½, 0, 0)      [edge-centre oxygen]",
  ],

  // -------------------------------------------------------------------
  // #167 R-3c  —  corundum (α-Al₂O₃, α-Fe₂O₃ hematite, Cr₂O₃)
  //               AND rhombohedral perovskite (LaAlO₃, LiNbO₃)
  // Hexagonal-setting Wyckoff labels used below.
  // -------------------------------------------------------------------
  167: [
    "unknown",
    "12c — cation / B-site  (0, 0, z)    [corundum Al/Fe/Cr; rhomb. perovskite B-site]",
    "6a  — cation / B-site  (0, 0, ¼)    [alternative; e.g. LiNbO₃ B-site]",
    "6b  — A-site            (0, 0, 0)    [rhombohedral perovskite A-site]",
    "18e — O anion           (x, 0, ¼)    [corundum / rhomb. perovskite oxygen]",
    "36f — general position",
  ],

  // -------------------------------------------------------------------
  // #62 Pnma  —  orthorhombic perovskite (GdFeO₃-type, most common distorted perovskite)
  //              Also: olivine, many oxides
  // -------------------------------------------------------------------
  62: [
    "unknown",
    "4a — B-site      (0, 0, 0)    [small cation; octahedrally coord e.g. Fe³⁺, Mn³⁺, Ti⁴⁺]",
    "4b — B-site alt. (0, 0, ½)   [alternative B-site]",
    "4c — A-site      (x, ¼, z)   [large cation; GdFeO₃-type Gd/La/Sr site]",
    "4c — O1 apical   (x, ¼, z)   [apical oxygen; same Wyckoff letter as A-site but different x,z]",
    "8d — O2 equatorial (x, y, z) [equatorial oxygen; two inequivalent O₂ per formula unit]",
  ],

  // -------------------------------------------------------------------
  // #141 I4₁/amd  —  tetragonal spinel (Jahn-Teller distorted; Mn₃O₄, CuFe₂O₄)
  //                   AND anatase TiO₂  AND scheelite (CaWO₄, AWO₄ tungstates)
  // -------------------------------------------------------------------
  141: [
    "unknown",
    "4a — A-site tetrahedral / Ti-site  (0, ¾, ⅛)   [spinel tet; anatase Ti]",
    "4b — A-site tetrahedral alt.       (0, ¼, ⅜)",
    "8c — B-site octahedral             (0, 0, ½)    [spinel oct; scheelite W-site]",
    "8d — B-site octahedral alt.        (0, ¾, ⅝)",
    "16h — O anion                      (x, y, z)    [spinel/anatase oxygen]",
    "4a — Ca-site (scheelite)           (0, ¼, ⅛)   [large 8-coord cation in CaWO₄-type]",
    "8e — O anion (anatase)             (0, 0, z)    [anatase O bridging site]",
  ],

  // -------------------------------------------------------------------
  // #136 P4₂/mnm  —  rutile TiO₂ (also VO₂, SnO₂, MnO₂, CrO₂)
  // -------------------------------------------------------------------
  136: [
    "unknown",
    "2a — cation site  (0, 0, 0)      [rutile-type cation; Ti⁴⁺, Sn⁴⁺, V⁴⁺]",
    "4f — O anion      (x, x, 0)      [rutile oxygen; 3-coord bridge]",
    "2b — cation site  (0, 0, ½)      [alternative cation site in related structures]",
  ],

  // -------------------------------------------------------------------
  // #166 R-3m  —  α-NaFeO₂-type layered oxide (LiCoO₂, NaMnO₂)
  //               AND delafossite (CuFeO₂) AND other layered structures
  // -------------------------------------------------------------------
  166: [
    "unknown",
    "3a — cation 1 (Li/Na)  (0, 0, 0)   [alkali-metal layer in layered oxide]",
    "3b — cation 2 (Co/Fe)  (0, 0, ½)   [transition-metal layer in layered oxide]",
    "6c — cation             (0, 0, z)   [general cation on 3-fold axis]",
    "9d — anion              (x, 0, ½)   [anion site]",
    "9e — anion              (x, 0, 0)   [anion site alt]",
    "18h — O anion           (x, -x, z)  [general hexagonal oxygen]",
  ],

  // -------------------------------------------------------------------
  // #194 P6₃/mmc  —  NiAs-type (NiAs, CoAs), wurtzite-like, some HCP metals
  //                   AND hexagonal BaTiO₃ (6H polytype)
  // -------------------------------------------------------------------
  194: [
    "unknown",
    "2a — site 1  (0, 0, 0)          [NiAs: As-site (FCC sublattice)]",
    "2b — site 2  (0, 0, ¼)          [alternative site]",
    "2c — site    (⅓, ⅔, ¼)         [NiAs: Ni-site (trigonal prismatic)]",
    "2d — site    (⅓, ⅔, ¾)         [alternative hexagonal site]",
    "4f — site    (⅓, ⅔, z)         [hexagonal perovskite face-sharing layer]",
    "4e — site    (0, 0, z)          [on 3-fold axis]",
    "6g — site    (½, 0, 0)          [edge midpoint]",
    "6h — site    (x, 2x, ¼)",
  ],

  // -------------------------------------------------------------------
  // #186 P6₃mc  —  wurtzite (ZnO, GaN, BeO type)
  // -------------------------------------------------------------------
  186: [
    "unknown",
    "2a — cation (Zn-site)  (0, 0, z)       [tetrahedral cation in ZnO/GaN]",
    "2b — anion  (O-site)   (⅓, ⅔, z+½)    [tetrahedral anion in wurtzite]",
  ],

  // -------------------------------------------------------------------
  // #206 Ia-3  —  bixbyite / C-type rare-earth sesquioxide
  //               (In₂O₃, Mn₂O₃, Y₂O₃, (La,Ce,...)₂O₃ high-T)
  //               AND weberite (Na₂MgAlF₇-type defect pyrochlore)
  // -------------------------------------------------------------------
  206: [
    "unknown",
    "8a  — cation site 1  (¼, ¼, ¼)   [C2-symmetric site; 6-coord; e.g. In(1) in In₂O₃]",
    "24d — cation site 2  (x, 0, ¼)   [S6-symmetric site; 6-coord; e.g. In(2) in In₂O₃]",
    "48e — O anion        (x, y, z)    [general anion position]",
  ],

  // -------------------------------------------------------------------
  // #229 Im-3m  —  BCC (W-type), CsCl-type, and β-brass-related oxides
  // -------------------------------------------------------------------
  229: [
    "unknown",
    "2a — main cation  (0, 0, 0)    [BCC origin site]",
    "6b — site         (0, ½, ½)    [face-centre set]",
    "8c — site         (¼, ¼, ¼)   [body-diagonal]",
    "12d — site        (¼, 0, ½)   [edge-centre]",
    "16f — site        (x, x, x)   [body-diagonal general]",
    "24h — anion       (x, x, z)   [general O position]",
    "48i — general",
  ],

  // -------------------------------------------------------------------
  // #230 Ia-3d  —  garnet (Y₃Al₅O₁₂ type; also some HEO garnets reported)
  // -------------------------------------------------------------------
  230: [
    "unknown",
    "24c — A-site (dodecahedral, 8-coord)  [large cation e.g. Y³⁺, Ca²⁺, Gd³⁺]",
    "16a — B-site (octahedral, 6-coord)    [medium cation e.g. Al³⁺, Fe³⁺, Cr³⁺]",
    "24d — C-site (tetrahedral, 4-coord)   [small cation e.g. Al³⁺, Si⁴⁺, Fe³⁺]",
    "96h — O anion (general position)      [oxygen in garnet]",
  ],

  // -------------------------------------------------------------------
  // #205 Pa-3  —  pyrite (FeS₂, CoS₂) and high-P NaCl phases
  // -------------------------------------------------------------------
  205: [
    "unknown",
    "4a — cation  (0, 0, 0)      [pyrite-type metal; e.g. Fe in FeS₂]",
    "4b — cation  (½, ½, ½)      [alternative cation]",
    "8c — cation  (x, x, x)      [general body-diagonal]",
    "24e — anion  (x, y, z)      [general anion; O or S]",
  ],

  // -------------------------------------------------------------------
  // #14 P2₁/c  —  baddeleyite (monoclinic ZrO₂; also monoclinic HfO₂, WO₃)
  // -------------------------------------------------------------------
  14: [
    "unknown",
    "4e — Zr-site  (x, y, z)   [Zr⁴⁺ in baddeleyite; 7-coord]",
    "4e — O1 site  (x, y, z)   [O(1) in baddeleyite (3 inequivalent O sites)]",
    "4e — O2 site  (x, y, z)   [O(2) in baddeleyite]",
    "4e — O3 site  (x, y, z)   [O(3) in baddeleyite]",
  ],

  // -------------------------------------------------------------------
  // #127 P4/mbm  —  tetragonal perovskite (a/a/c⁺ octahedral tilt)
  //                  e.g. SrTiO₃ at ~105 K, CaTiO₃ intermediate
  // -------------------------------------------------------------------
  127: [
    "unknown",
    "2a — B-site  (0, 0, 0)    [B-site cation octahedral]",
    "2b — B-site  (0, 0, ½)    [B-site alt]",
    "2c — A-site  (0, ½, ½)    [A-site; 12-coord]",
    "2d — A-site  (0, ½, 0)    [A-site alt]",
    "4g — O1 equatorial  (x, x+½, 0)  [in-plane oxygen]",
    "2e — O2 apical      (0, 0, z)    [apical oxygen]",
  ],

  // -------------------------------------------------------------------
  // #140 I4/mcm  —  tetragonal perovskite (a/a/c⁻ tilt; BaTiO₃ tetragonal, SrTiO₃ 105K)
  // -------------------------------------------------------------------
  140: [
    "unknown",
    "4a — B-site  (0, 0, ¼)    [B-site octahedral cation]",
    "4b — B-site  (0, 0, 0)    [B-site alt]",
    "4c — A-site  (0, ½, 0)    [A-site 12-coord]",
    "4d — A-site  (0, ½, ¼)    [A-site alt]",
    "8h — O equatorial  (x, x+½, 0)  [equatorial oxygen]",
    "4e — O apical      (0, 0, z)    [apical oxygen]",
  ],

  // -------------------------------------------------------------------
  // #139 I4/mmm  —  body-centred tetragonal; D0₂₂/L1₀-related; layered perovskite
  //                  Ruddlesden–Popper phase first layer (A₂BO₄ K₂NiF₄-type → #139)
  // -------------------------------------------------------------------
  139: [
    "unknown",
    "2a — site (0,0,0)         [BCC-like corner cation]",
    "2b — site (0,0,½)         [body-centre cation]",
    "4c — site (0,½,0)         [K₂NiF₄-type A-site (Ba/Sr/La)]",
    "4d — site (0,½,¼)         [alternative A-site]",
    "4e — B-site (0,0,z)       [Ni/Cu/Co in K₂NiF₄-type; 6-coord]",
    "8g — equatorial O  (x,½,0)  [in-plane O in K₂NiF₄-type]",
    "4e — apical O (0,0,z)",
  ],
};

// ---------------------------------------------------------------------------
// Fallback generic labels when the space group is not in WYCKOFF_SITES
// Keyed by structure family name (lowercase).
// ---------------------------------------------------------------------------
const FAMILY_FALLBACK_SITES = {
  rocksalt:   ["unknown", "M-site (cation)", "X-site (anion)"],
  spinel:     ["unknown", "A-site (tetrahedral)", "B-site (octahedral)", "O-site (anion)"],
  pyrochlore: ["unknown", "A-site", "B-site", "O-site (48f)", "O'-site (8b)"],
  perovskite: ["unknown", "A-site (large cation)", "B-site (small cation)", "O-site (anion)"],
  fluorite:   ["unknown", "Cation site", "Anion site"],
  other:      ["unknown", "A-site", "B-site", "C-site", "X-site (anion)"],
  unknown:    ["unknown", "A-site", "B-site", "X-site (anion)"],
};

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Return the Wyckoff site options for the given space group number, falling back
 * to structure-family generic labels when the SG isn't in our database.
 */
export function getSiteOptions(sgNum, structureFamily) {
  if (sgNum && WYCKOFF_SITES[sgNum]) return WYCKOFF_SITES[sgNum];
  const sf = (structureFamily || "other").toLowerCase();
  return FAMILY_FALLBACK_SITES[sf] || FAMILY_FALLBACK_SITES.other;
}

/**
 * Wire up the Space Group input and Crystallographic Site Assignments UI.
 *
 * @param {Object} opts
 * @param {function(): Array}  opts.getElements        - () => [[symbol, ratio], ...]
 * @param {function(): string} opts.getStructureFamily - () => current structure family
 * @returns {{ rebuildSiteRows: function, serializeSites: function } | null}
 */
export function setupSpacegroupUI({ getElements, getStructureFamily }) {
  const sgEl       = document.getElementById("spacegroup");
  const sitesField = document.getElementById("element-sites-json");

  if (!sgEl) return null;

  let tsInstance = null;

  // -------------------------------------------------------------------
  // Tom Select helpers
  // -------------------------------------------------------------------

  function buildTsOptions() {
    const sf        = getStructureFamily() || "other";
    const suggested = new Set(SG_SUGGESTIONS_BY_FAMILY[sf] || []);
    const opts = [
      { value: "", text: "Unknown / not specified", optgroup: "__none__" },
    ];
    ALL_SPACE_GROUPS.forEach(([n, hm]) => {
      opts.push({
        value:    `${hm} (#${n})`,
        text:     `${hm} (#${n})`,
        num:      String(n),
        optgroup: suggested.has(n) ? "suggested" : "all",
      });
    });
    return opts;
  }

  function buildOptgroups() {
    const sf      = getStructureFamily() || "other";
    const sfLabel = sf.charAt(0).toUpperCase() + sf.slice(1);
    return [
      { value: "__none__",  label: "" },
      { value: "suggested", label: `Common for ${sfLabel}` },
      { value: "all",       label: "All 230 space groups" },
    ];
  }

  function initTomSelect(preserveValue) {
    const TomSelectClass = window.TomSelect;
    if (!TomSelectClass) return;   // graceful degradation

    if (tsInstance) {
      tsInstance.destroy();
      tsInstance = null;
    }

    tsInstance = new TomSelectClass(sgEl, {
      options:           buildTsOptions(),
      optgroups:         buildOptgroups(),
      optgroupField:     "optgroup",
      valueField:        "value",
      labelField:        "text",
      searchField:       ["text", "num"],
      lockOptgroupOrder: true,
      allowEmptyOption:  true,
      placeholder:       "Search by name or number…",
      maxOptions:        null,
      render: {
        option: (data, escape) =>
          `<div class="py-1">${escape(data.text)}</div>`,
        item: (data, escape) =>
          `<div>${escape(data.text) || "Unknown / not specified"}</div>`,
        optgroup_header: (data, escape) => {
          if (data.value === "__none__") return "";
          if (data.value === "suggested") {
            const sf = getStructureFamily() || "other";
            const sfLabel = sf.charAt(0).toUpperCase() + sf.slice(1);
            return `<div class="optgroup-header small fw-semibold text-muted px-2 pt-2 pb-1">Common for ${escape(sfLabel)}</div>`;
          }
          return `<div class="optgroup-header small fw-semibold text-muted px-2 pt-2 pb-1">${escape(data.label)}</div>`;
        },
      },
      onChange: () => rebuildSiteRows(),
    });

    const target = preserveValue !== undefined
      ? preserveValue
      : (sgEl.dataset.initialValue || "");
    if (target) tsInstance.setValue(target, /* silent = */ true);
  }

  function getCurrentValue() {
    return tsInstance ? tsInstance.getValue() : sgEl.value;
  }

  // -------------------------------------------------------------------
  // Snapshot & restore site selections across rebuilds
  // -------------------------------------------------------------------

  function snapshotSites() {
    const snap = {};
    if (sitesField && sitesField.value) {
      try { Object.assign(snap, JSON.parse(sitesField.value)); } catch { /* ok */ }
    }
    // live selects always win over the stale hidden field
    document.querySelectorAll(".sg-site-select").forEach(sel => {
      const sym = sel.closest("[data-sym]")?.dataset?.sym;
      if (sym) snap[sym] = sel.value;
    });
    return snap;
  }

  function makeSiteSelect(sym, options, prev) {
    const existing = prev[sym] || "unknown";
    const selected = options.includes(existing) ? existing : "unknown";
    const sel = document.createElement("select");
    sel.className = "form-select sg-site-select";
    sel.setAttribute("aria-label", `Crystallographic site for ${sym}`);
    options.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s;
      if (s === "unknown") {
        opt.textContent = "Unknown";
      } else if (s.includes("[")) {
        // Wyckoff format: "8a  — A-site tetrahedral   (x,x,x)  [chemistry note]"
        // Strip the [..] note then anything after 3+ spaces (the coordinate block)
        opt.textContent = s.replace(/\s*\[.*/, "").replace(/\s{3,}.*/, "").trim();
      } else {
        // Generic fallback label (e.g. "A-site (tetrahedral)") — keep as-is
        opt.textContent = s;
      }
      if (s === selected) opt.selected = true;
      sel.appendChild(opt);
    });
    return sel;
  }

  // -------------------------------------------------------------------
  // Core rebuild — injects site selects into both the unlocked periodic-
  // table rows AND the locked composition table cells.
  // -------------------------------------------------------------------

  function rebuildSiteRows() {
    const prev    = snapshotSites();
    const elems   = getElements();
    const sgNum   = parseSpacegroupNum(getCurrentValue());
    const sf      = getStructureFamily();
    const options = getSiteOptions(sgNum, sf);

    // ── 1. Unlocked: inject into #selected-elements input-group rows ──
    const selectedEl = document.getElementById("selected-elements");
    if (selectedEl) {
      // Remove stale injected selects first
      selectedEl.querySelectorAll(".sg-site-select").forEach(el => el.remove());

      elems.forEach(([sym]) => {
        const row = selectedEl.querySelector(`[data-sym="${sym}"]`);
        if (!row) return;
        const sel = makeSiteSelect(sym, options, prev);
        // Insert before the × button so order is: badge | ratio | site | ×
        const removeBtn = row.querySelector("button");
        row.insertBefore(sel, removeBtn ?? null);
      });

      // Show/hide column headers
      const headers = document.getElementById("comp-col-headers");
      if (headers) headers.classList.toggle("d-none", elems.length === 0);
    }

    // ── 2. Locked: inject into .site-cell table cells ─────────────────
    const lockedTable = document.getElementById("locked-composition-table");
    if (lockedTable) {
      lockedTable.querySelectorAll("tbody tr[data-sym]").forEach(tr => {
        const sym  = tr.dataset.sym;
        const cell = tr.querySelector(".site-cell");
        if (!cell) return;
        cell.innerHTML = "";
        const sel = makeSiteSelect(sym, options, prev);
        sel.className = "form-select sg-site-select";
        cell.appendChild(sel);
      });
    }

    serializeSites();
  }

  function serializeSites() {
    if (!sitesField) return;
    const out = {};
    document.querySelectorAll(".sg-site-select").forEach(sel => {
      const sym = sel.closest("[data-sym]")?.dataset?.sym;
      if (sym) out[sym] = sel.value;
    });
    sitesField.value = JSON.stringify(out);
  }

  // -------------------------------------------------------------------
  // Event wiring
  // -------------------------------------------------------------------

  // Structure family change → rebuild Tom Select groups + refresh sites
  const sfSelect = document.getElementById("structure_family");
  if (sfSelect) {
    sfSelect.addEventListener("change", () => {
      initTomSelect(getCurrentValue());
      rebuildSiteRows();
    });
  }

  // Delegate all .sg-site-select changes (they live in different containers)
  document.addEventListener("change", e => {
    if (e.target.classList.contains("sg-site-select")) serializeSites();
  });

  // -------------------------------------------------------------------
  // Initial render
  // -------------------------------------------------------------------
  initTomSelect();
  rebuildSiteRows();

  return { rebuildSiteRows, serializeSites };
}
