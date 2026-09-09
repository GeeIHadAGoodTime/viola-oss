/**
 * Minimal QR Code Generator
 * Supports: Byte mode, versions 1-10, Medium error correction
 * No external dependencies.
 *
 * Usage:
 *   import { generateQR } from '../utils/qrcode';
 *   const modules = generateQR('https://example.com'); // 2D boolean array
 */

// =============================================================================
// Galois Field GF(256) arithmetic (primitive polynomial 0x11D)
// =============================================================================

const GF_EXP = new Array(512);
const GF_LOG = new Array(256);

{
  let x = 1;
  for (let i = 0; i < 255; i++) {
    GF_EXP[i] = x;
    GF_LOG[x] = i;
    x <<= 1;
    if (x >= 256) x ^= 0x11D;
  }
  for (let i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255];
}

function gfMul(a, b) {
  if (a === 0 || b === 0) return 0;
  return GF_EXP[GF_LOG[a] + GF_LOG[b]];
}

// =============================================================================
// Reed-Solomon error correction
// =============================================================================

function rsGeneratorPoly(numEC) {
  // g(x) = (x - a^0)(x - a^1)...(x - a^{numEC-1})
  // Coefficients stored highest degree first: [x^n, x^{n-1}, ..., x^0]
  let gen = [1];
  for (let i = 0; i < numEC; i++) {
    const next = new Array(gen.length + 1).fill(0);
    for (let j = 0; j < gen.length; j++) {
      next[j] ^= gen[j]; // * x
      next[j + 1] ^= gfMul(gen[j], GF_EXP[i]); // * alpha^i
    }
    gen = next;
  }
  return gen;
}

function rsEncode(data, numEC) {
  const gen = rsGeneratorPoly(numEC);
  const padded = [...data, ...new Array(numEC).fill(0)];

  for (let i = 0; i < data.length; i++) {
    const coef = padded[i];
    if (coef !== 0) {
      for (let j = 0; j < gen.length; j++) {
        padded[i + j] ^= gfMul(gen[j], coef);
      }
    }
  }
  return padded.slice(data.length);
}

// =============================================================================
// QR Version tables (Medium EC level, byte mode)
// =============================================================================

// capacity: max byte-mode characters; dataCodewords: total data bytes;
// ecPerBlock: EC codewords per RS block; blocks: [[count, dataPerBlock], ...]
const VERSION_INFO = [
  null, // index 0 unused
  { capacity: 14,  dataCodewords: 16,  ecPerBlock: 10, blocks: [[1, 16]] },
  { capacity: 26,  dataCodewords: 28,  ecPerBlock: 16, blocks: [[1, 28]] },
  { capacity: 42,  dataCodewords: 44,  ecPerBlock: 26, blocks: [[1, 44]] },
  { capacity: 62,  dataCodewords: 64,  ecPerBlock: 18, blocks: [[2, 32]] },
  { capacity: 84,  dataCodewords: 86,  ecPerBlock: 24, blocks: [[2, 43]] },
  { capacity: 106, dataCodewords: 108, ecPerBlock: 16, blocks: [[4, 27]] },
  { capacity: 122, dataCodewords: 124, ecPerBlock: 18, blocks: [[4, 31]] },
  { capacity: 152, dataCodewords: 154, ecPerBlock: 22, blocks: [[2, 38], [2, 39]] },
  { capacity: 180, dataCodewords: 182, ecPerBlock: 22, blocks: [[3, 36], [2, 37]] },
  { capacity: 213, dataCodewords: 216, ecPerBlock: 26, blocks: [[4, 43], [1, 44]] },
];

// Alignment pattern center positions per version
const ALIGNMENT = [
  null, [], [6,18], [6,22], [6,26], [6,30],
  [6,34], [6,22,38], [6,24,42], [6,26,46], [6,28,50],
];

// =============================================================================
// Data encoding (byte mode)
// =============================================================================

function pushBits(arr, value, count) {
  for (let i = count - 1; i >= 0; i--) arr.push((value >> i) & 1);
}

function encodeData(text, version) {
  const info = VERSION_INFO[version];
  const totalBits = info.dataCodewords * 8;
  const bytes = new TextEncoder().encode(text);
  const bits = [];

  pushBits(bits, 0b0100, 4); // byte mode indicator
  pushBits(bits, bytes.length, version <= 9 ? 8 : 16); // character count
  for (const b of bytes) pushBits(bits, b, 8); // data

  // Terminator
  pushBits(bits, 0, Math.min(4, totalBits - bits.length));
  while (bits.length % 8 !== 0) bits.push(0); // byte-align

  // Pad codewords
  const padBytes = [0xEC, 0x11];
  let pi = 0;
  while (bits.length < totalBits) { pushBits(bits, padBytes[pi++ % 2], 8); }
  bits.length = totalBits;

  // Convert to byte array
  const codewords = [];
  for (let i = 0; i < totalBits; i += 8) {
    let v = 0;
    for (let j = 0; j < 8; j++) v = (v << 1) | (bits[i + j] || 0);
    codewords.push(v);
  }
  return codewords;
}

// Split into blocks, compute EC, interleave
function buildCodewords(dataCodewords, version) {
  const info = VERSION_INFO[version];
  const blocks = [];
  let offset = 0;

  for (const [count, dataPerBlock] of info.blocks) {
    for (let i = 0; i < count; i++) {
      const blockData = dataCodewords.slice(offset, offset + dataPerBlock);
      blocks.push({ data: blockData, ec: rsEncode(blockData, info.ecPerBlock) });
      offset += dataPerBlock;
    }
  }

  // Interleave data
  const result = [];
  const maxData = Math.max(...blocks.map(b => b.data.length));
  for (let i = 0; i < maxData; i++) {
    for (const block of blocks) {
      if (i < block.data.length) result.push(block.data[i]);
    }
  }
  // Interleave EC
  for (let i = 0; i < info.ecPerBlock; i++) {
    for (const block of blocks) {
      if (i < block.ec.length) result.push(block.ec[i]);
    }
  }
  return result;
}

// =============================================================================
// Matrix construction
// =============================================================================

function placeFinderPattern(matrix, reserved, row, col, size) {
  for (let r = -1; r <= 7; r++) {
    for (let c = -1; c <= 7; c++) {
      const rr = row + r, cc = col + c;
      if (rr < 0 || rr >= size || cc < 0 || cc >= size) continue;
      reserved[rr][cc] = true;
      if (r >= 0 && r <= 6 && c >= 0 && c <= 6) {
        matrix[rr][cc] = (r === 0 || r === 6 || c === 0 || c === 6) ||
                          (r >= 2 && r <= 4 && c >= 2 && c <= 4);
      }
    }
  }
}

function placeFinders(matrix, reserved, size) {
  placeFinderPattern(matrix, reserved, 0, 0, size);
  placeFinderPattern(matrix, reserved, 0, size - 7, size);
  placeFinderPattern(matrix, reserved, size - 7, 0, size);
}

function placeTimingPatterns(matrix, reserved, size) {
  for (let i = 8; i < size - 8; i++) {
    matrix[6][i] = i % 2 === 0;
    reserved[6][i] = true;
    matrix[i][6] = i % 2 === 0;
    reserved[i][6] = true;
  }
}

function placeAlignmentPatterns(matrix, reserved, version, size) {
  const positions = ALIGNMENT[version];
  if (!positions || positions.length === 0) return;

  for (const row of positions) {
    for (const col of positions) {
      // Skip overlap with finder patterns
      if (row <= 8 && col <= 8) continue;       // top-left
      if (row <= 8 && col >= size - 8) continue; // top-right
      if (row >= size - 8 && col <= 8) continue;  // bottom-left

      for (let r = -2; r <= 2; r++) {
        for (let c = -2; c <= 2; c++) {
          matrix[row + r][col + c] =
            Math.abs(r) === 2 || Math.abs(c) === 2 || (r === 0 && c === 0);
          reserved[row + r][col + c] = true;
        }
      }
    }
  }
}

function reserveFormatArea(reserved, size) {
  // Around top-left finder
  for (let i = 0; i <= 8; i++) {
    reserved[i][8] = true;
    reserved[8][i] = true;
  }
  // Around bottom-left finder
  for (let i = 0; i < 7; i++) reserved[size - 1 - i][8] = true;
  // Around top-right finder
  for (let i = 0; i < 8; i++) reserved[8][size - 1 - i] = true;
  // Dark module
  reserved[size - 8][8] = true;
}

function reserveVersionArea(reserved, size, version) {
  if (version < 7) return;
  for (let i = 0; i < 6; i++) {
    for (let j = 0; j < 3; j++) {
      reserved[size - 11 + j][i] = true;
      reserved[i][size - 11 + j] = true;
    }
  }
}

// Place data bits in zigzag pattern
function placeData(matrix, reserved, size, dataBits) {
  let bitIdx = 0;
  let upward = true;

  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5; // skip timing column

    const start = upward ? size - 1 : 0;
    const end = upward ? -1 : size;
    const step = upward ? -1 : 1;

    for (let row = start; row !== end; row += step) {
      for (let dc = 0; dc <= 1; dc++) {
        const col = right - dc;
        if (col < 0 || reserved[row][col]) continue;
        matrix[row][col] = bitIdx < dataBits.length && dataBits[bitIdx] === 1;
        bitIdx++;
      }
    }
    upward = !upward;
  }
}

// =============================================================================
// Masking
// =============================================================================

function getMaskBit(mask, row, col) {
  switch (mask) {
    case 0: return (row + col) % 2 === 0;
    case 1: return row % 2 === 0;
    case 2: return col % 3 === 0;
    case 3: return (row + col) % 3 === 0;
    case 4: return (Math.floor(row / 2) + Math.floor(col / 3)) % 2 === 0;
    case 5: return (row * col) % 2 + (row * col) % 3 === 0;
    case 6: return ((row * col) % 2 + (row * col) % 3) % 2 === 0;
    case 7: return ((row + col) % 2 + (row * col) % 3) % 2 === 0;
    default: return false;
  }
}

function calculatePenalty(matrix, size) {
  let penalty = 0;

  // Rule 1: Runs of 5+ same-colored modules
  for (let i = 0; i < size; i++) {
    let rRun = 1, cRun = 1;
    for (let j = 1; j < size; j++) {
      // Row
      if (matrix[i][j] === matrix[i][j - 1]) {
        rRun++;
        if (rRun === 5) penalty += 3;
        else if (rRun > 5) penalty += 1;
      } else { rRun = 1; }
      // Column
      if (matrix[j][i] === matrix[j - 1][i]) {
        cRun++;
        if (cRun === 5) penalty += 3;
        else if (cRun > 5) penalty += 1;
      } else { cRun = 1; }
    }
  }

  // Rule 2: 2x2 same-colored blocks
  for (let i = 0; i < size - 1; i++) {
    for (let j = 0; j < size - 1; j++) {
      const v = matrix[i][j];
      if (v === matrix[i][j+1] && v === matrix[i+1][j] && v === matrix[i+1][j+1]) {
        penalty += 3;
      }
    }
  }

  // Rule 3: Finder-like patterns (1011101 with 4 whites on either side)
  const p1 = [true,false,true,true,true,false,true,false,false,false,false];
  const p2 = [false,false,false,false,true,false,true,true,true,false,true];
  for (let i = 0; i < size; i++) {
    for (let j = 0; j <= size - 11; j++) {
      let m1r = true, m2r = true, m1c = true, m2c = true;
      for (let k = 0; k < 11; k++) {
        if (matrix[i][j+k] !== p1[k]) m1r = false;
        if (matrix[i][j+k] !== p2[k]) m2r = false;
        if (matrix[j+k][i] !== p1[k]) m1c = false;
        if (matrix[j+k][i] !== p2[k]) m2c = false;
      }
      if (m1r) penalty += 40;
      if (m2r) penalty += 40;
      if (m1c) penalty += 40;
      if (m2c) penalty += 40;
    }
  }

  // Rule 4: Dark/light module proportion
  let dark = 0;
  for (let i = 0; i < size; i++)
    for (let j = 0; j < size; j++)
      if (matrix[i][j]) dark++;
  const pct = (dark * 100) / (size * size);
  const prev5 = Math.floor(pct / 5) * 5;
  const next5 = prev5 + 5;
  penalty += Math.min(Math.abs(prev5 - 50) / 5, Math.abs(next5 - 50) / 5) * 10;

  return penalty;
}

// =============================================================================
// Format & version information
// =============================================================================

function getFormatBits(maskPattern) {
  // EC level M = 0b00
  let data = (0b00 << 3) | maskPattern;
  let bits = data << 10;
  // BCH(15,5) generator: x^10 + x^8 + x^5 + x^4 + x^2 + x + 1 = 0x537
  for (let i = 4; i >= 0; i--) {
    if (bits & (1 << (i + 10))) bits ^= 0x537 << i;
  }
  return ((data << 10) | bits) ^ 0x5412;
}

function placeFormatInfo(matrix, size, formatBits) {
  // Copy 1: around top-left finder
  const pos1 = [
    [0,8],[1,8],[2,8],[3,8],[4,8],[5,8],[7,8],[8,8],
    [8,7],[8,5],[8,4],[8,3],[8,2],[8,1],[8,0],
  ];
  // Copy 2: bottom-left column + top-right row
  const pos2 = [
    [size-1,8],[size-2,8],[size-3,8],[size-4,8],
    [size-5,8],[size-6,8],[size-7,8],
    [8,size-8],[8,size-7],[8,size-6],[8,size-5],
    [8,size-4],[8,size-3],[8,size-2],[8,size-1],
  ];

  for (let i = 0; i < 15; i++) {
    const bit = ((formatBits >> i) & 1) === 1;
    matrix[pos1[i][0]][pos1[i][1]] = bit;
    matrix[pos2[i][0]][pos2[i][1]] = bit;
  }
  matrix[size - 8][8] = true; // always-dark module
}

function getVersionBits(version) {
  if (version < 7) return -1;
  let bits = version << 12;
  // BCH(18,6) generator: 0x1F25
  for (let i = 5; i >= 0; i--) {
    if (bits & (1 << (i + 12))) bits ^= 0x1F25 << i;
  }
  return (version << 12) | bits;
}

function placeVersionInfo(matrix, size, version) {
  if (version < 7) return;
  const bits = getVersionBits(version);
  let idx = 0;
  for (let i = 0; i < 6; i++) {
    for (let j = 0; j < 3; j++) {
      const bit = ((bits >> idx) & 1) === 1;
      matrix[size - 11 + j][i] = bit;
      matrix[i][size - 11 + j] = bit;
      idx++;
    }
  }
}

// =============================================================================
// Main entry point
// =============================================================================

/**
 * Generate a QR code for the given text.
 * @param {string} text - The text/URL to encode
 * @returns {boolean[][]} 2D array where true = dark module
 */
export function generateQR(text) {
  const bytes = new TextEncoder().encode(text);

  // Find minimum version
  let version = 1;
  while (version <= 10 && bytes.length > VERSION_INFO[version].capacity) version++;
  if (version > 10) throw new Error('Text too long for QR versions 1-10');

  const size = version * 4 + 17;

  // Encode data and build codewords with EC
  const dataCodewords = encodeData(text, version);
  const allCodewords = buildCodewords(dataCodewords, version);

  // Convert codewords to bit array
  const dataBits = [];
  for (const cw of allCodewords) pushBits(dataBits, cw, 8);

  // Try all 8 mask patterns, pick the one with lowest penalty
  let bestMatrix = null;
  let bestPenalty = Infinity;

  for (let mask = 0; mask < 8; mask++) {
    const matrix = Array.from({ length: size }, () => new Array(size).fill(false));
    const reserved = Array.from({ length: size }, () => new Array(size).fill(false));

    // 1. Place function patterns
    placeFinders(matrix, reserved, size);
    placeTimingPatterns(matrix, reserved, size);
    placeAlignmentPatterns(matrix, reserved, version, size);
    reserveFormatArea(reserved, size);
    reserveVersionArea(reserved, size, version);

    // 2. Place data
    placeData(matrix, reserved, size, dataBits);

    // 3. Apply mask to data modules only
    for (let r = 0; r < size; r++) {
      for (let c = 0; c < size; c++) {
        if (!reserved[r][c] && getMaskBit(mask, r, c)) {
          matrix[r][c] = !matrix[r][c];
        }
      }
    }

    // 4. Place format/version info (after masking)
    placeFormatInfo(matrix, size, getFormatBits(mask));
    placeVersionInfo(matrix, size, version);

    // 5. Score
    const penalty = calculatePenalty(matrix, size);
    if (penalty < bestPenalty) {
      bestPenalty = penalty;
      bestMatrix = matrix;
    }
  }

  return bestMatrix;
}
