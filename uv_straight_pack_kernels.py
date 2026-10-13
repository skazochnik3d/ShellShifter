"""Independent device-resident rotation, Euclidean padding and batched fits.

Descriptor fields: width, height, raw stride/offset, padded width/height,
padded stride/offset, row prefix offset, run offset/count, turn, tie rank,
maximum X/Y origin, reserved. Runs are inclusive integer intervals.
"""
SOURCE = r'''
// One workgroup per complete shelf variation. Existing rows accept either
// allowed orientation, with a best-fit width reduction performed on-device.
// Double coordinates avoid cell rounding in this conservative rectangle seed.
__kernel void shelf_layouts(__global const double2 *sizes, __global const int *flags,
    __global const int *orders, __global double *rows, __global int2 *state,
    __global double *output, int count, double extent_x, double extent_y, int first, int chunk) {
    int slot=get_group_id(0), lid=get_local_id(0);
    if (!state[slot].x) return;
    __local ulong keys[256];
    __local int row_count, failed, chosen_turn, chosen_row;
    if (!lid) { row_count=state[slot].y; failed=0; }
    barrier(CLK_LOCAL_MEM_FENCE);
    for (int at=first;at<first+chunk;++at) {
        int index=orders[slot*count+at];double2 box=sizes[index];
        ulong best=0xffffffffffffffffUL;
        for (int turn=0;turn<2;++turn) {
            if (!(flags[index] & (1<<turn))) continue;
            double w=turn?box.y:box.x, h=turn?box.x:box.y;
            for (int r=lid;r<row_count;r+=256) {
                int offset=(slot*count+r)*3;
                double capacity=extent_x-rows[offset+2];
                if (h<=rows[offset+1]+1e-12 && w<=capacity+1e-12) {
                    ulong score=(ulong)(fmax(0.0,capacity-w)*1e9);
                    best=min(best,(score*(ulong)count+(ulong)r)*2+(ulong)turn);
                }
            }
        }
        keys[lid]=best;
        barrier(CLK_LOCAL_MEM_FENCE);
        for (int span=128;span;span/=2) {
            if (lid<span) keys[lid]=min(keys[lid],keys[lid+span]);
            barrier(CLK_LOCAL_MEM_FENCE);
        }
        ulong key=keys[0];
        if (!lid) {
            int turn=(int)(key & 1), r=(int)((key/2)%(ulong)count);
            if (key==0xffffffffffffffffUL) {
                turn=-1;
                for (int t=0;t<2;++t) {
                    if (!(flags[index] & (1<<t))) continue;
                    double w=t?box.y:box.x,h=t?box.x:box.y;
                    if (w<=extent_x+1e-12 && h<=extent_y+1e-12 &&
                        (turn<0 || h<(turn?box.x:box.y))) turn=t;
                }
                double y=0.0;
                if (row_count) { int prev=(slot*count+row_count-1)*3; y=rows[prev]+rows[prev+1]; }
                double h=turn?box.x:box.y;
                if (turn<0 || y+h>extent_y+1e-12) failed=1;
                else {
                    r=row_count++;int offset=(slot*count+r)*3;
                    rows[offset]=y;rows[offset+1]=h;rows[offset+2]=0.0;
                }
            }
            chosen_turn=turn;chosen_row=r;
        }
        barrier(CLK_LOCAL_MEM_FENCE);
        if (failed) break;
        if (!lid) {
            int r=(slot*count+chosen_row)*3;
            int offset=(slot*count+index)*3;
            output[offset]=rows[r+2];output[offset+1]=rows[r];output[offset+2]=chosen_turn;
            rows[r+2]+=chosen_turn?box.y:box.x;
        }
        barrier(CLK_LOCAL_MEM_FENCE);
    }
    if (!lid) state[slot]=(int2)(!failed,row_count);
}

// Solid rectangles have one run per padded row. No triangle raster, rotation,
// run counting, prefix scan, or host readback is needed to prepare them.
__kernel void solid_shapes(__global uint *raw, __global int4 *runs,
                          __global const int *descriptors,
                          __global const int *radii, int count, int gap) {
    int word=get_global_id(0), row=get_global_id(1), variant=get_global_id(2);
    if (variant>=count) return;
    __global const int *d=descriptors+variant*16;
    if (row<d[1] && word<d[2]) {
        int remaining=d[0]-word*32;
        raw[d[3]+row*d[2]+word]=remaining>=32 ? 0xffffffffu : (1u<<remaining)-1u;
    }
    if (!word && row<d[5]) {
        int distance=max(0,max(gap-row,row-(gap+d[1]-1)));
        int radius=radii[gap+distance];
        runs[d[9]+row]=(int4)(row-gap,-radius,d[0]-1+radius,0);
    }
}

inline uint fetch_word(__global const uint *data, int offset, int stride,
                       int height, int row, int word) {
    if (row < 0 || row >= height || word < 0 || word >= stride) return 0u;
    return data[offset + row * stride + word];
}

inline uint aligned_word(__global const uint *data, int offset, int stride,
                         int height, int row, int x) {
    int word = x >> 5, shift = x & 31;
    uint a = fetch_word(data, offset, stride, height, row, word);
    if (!shift) return a;
    uint b = fetch_word(data, offset, stride, height, row, word + 1);
    return (a >> shift) | (b << (32 - shift));
}

inline uint dilated_word(__global const uint *raw, int offset, int stride,
                         int height, int row, int x, int radius) {
    uint result = 0u;
    if (radius < 16) {
        for (int dx = -radius; dx <= radius; ++dx)
            result |= aligned_word(raw,offset,stride,height,row,x + dx);
    } else {
        // At radius >= 16 the dilation of any nonzero 32-bit source word
        // is one interval; interior gaps within that word are fully covered.
        int first = max(0,(x - radius) >> 5);
        int last = min(stride - 1,(x + 31 + radius) >> 5);
        for (int word = first; word <= last; ++word) {
            uint bits = fetch_word(raw,offset,stride,height,row,word);
            if (!bits) continue;
            int lo = word * 32 + 31 - (int)clz(bits & (0u - bits)) - radius - x;
            int hi = word * 32 + 31 - (int)clz(bits) + radius - x;
            if (lo > 31 || hi < 0) continue;
            lo = max(0,lo); hi = min(31,hi);
            result |= (0xffffffffu << lo) & (0xffffffffu >> (31 - hi));
            if (result == 0xffffffffu) break;
        }
    }
    return result;
}

__kernel void raster_shell(__global const double2 *triangles, int offset,
                           int count, double scale, double grid,
                           int width, int height, int stride,
                           __global uint *raw) {
    int t = get_global_id(0), row = get_global_id(1);
    if (t >= count || row >= height) return;
    double2 polygon[8], temp[8];
    int n = 3;
    for (int i = 0; i < 3; ++i) polygon[i] = (triangles[(offset + t) * 3 + i] * scale) * grid;
    double low = fmin(polygon[0].y, fmin(polygon[1].y, polygon[2].y));
    double high = fmax(polygon[0].y, fmax(polygon[1].y, polygon[2].y));
    if (row < floor(low - 1e-10) || row >= floor(high + 1e-10) + 1) return;
    for (int side = 0; side < 2 && n; ++side) {
        double bound = row + side;
        int out = 0;
        double2 previous = polygon[n - 1];
        int before = side ? previous.y <= bound : previous.y >= bound;
        for (int i = 0; i < n; ++i) {
            double2 point = polygon[i];
            int inside = side ? point.y <= bound : point.y >= bound;
            if (inside != before) {
                double fraction = (bound - previous.y) / (point.y - previous.y);
                temp[out++] = (double2)(previous.x + fraction * (point.x - previous.x), bound);
            }
            if (inside) temp[out++] = point;
            previous = point; before = inside;
        }
        n = out;
        for (int i = 0; i < n; ++i) polygon[i] = temp[i];
    }
    if (!n) return;
    double left = polygon[0].x, right = left;
    for (int i = 1; i < n; ++i) {
        left = fmin(left, polygon[i].x); right = fmax(right, polygon[i].x);
    }
    int a = max(0, (int)floor(left - 1e-10));
    int b = min(width - 1, (int)floor(right + 1e-10));
    if (a > b) return;
    int first = a >> 5, last = b >> 5;
    for (int word = first; word <= last; ++word) {
        uint bits = 0xffffffffu;
        if (word == first) bits &= 0xffffffffu << (a & 31);
        if (word == last) bits &= 0xffffffffu >> (31 - (b & 31));
        atomic_or(raw + row * stride + word, bits);
    }
}

__kernel void rotate_shapes(__global uint *raw, __global const int *desc, int count) {
    int word = get_global_id(0), row = get_global_id(1), variant = get_global_id(2);
    if (!variant || variant >= count) return;
    __global const int *d = desc + variant * 16;
    if (word >= d[2] || row >= d[1]) return;
    int w = desc[0], h = desc[1], base_stride = desc[2];
    uint result = 0u;
    for (int bit = 0; bit < 32; ++bit) {
        int x = word * 32 + bit;
        if (x >= d[0]) break;
        int sx, sy;
        if (d[11] == 1) { sx = row; sy = h - 1 - x; }
        else if (d[11] == 2) { sx = w - 1 - x; sy = h - 1 - row; }
        else { sx = w - 1 - row; sy = x; }
        if (raw[sy * base_stride + (sx >> 5)] & (1u << (sx & 31))) result |= 1u << bit;
    }
    raw[d[3] + row * d[2] + word] = result;
}

__kernel void pad_shapes(__global const uint *raw, __global uint *padded,
                         __global const int *desc, __global const int *widths,
                         int count, int gap, int first_row) {
    int word = get_global_id(0), row = first_row + get_global_id(1), variant = get_global_id(2);
    if (variant >= count) return;
    __global const int *d = desc + variant * 16;
    if (word >= d[6] || row >= d[5]) return;
    int x = word * 32 - gap, y = row - gap;
    uint value = 0u;
    for (int dy = -gap; dy <= gap; ++dy) {
        int yy = y - dy;
        if (yy < 0 || yy >= d[1]) continue;
        int radius = widths[dy + gap];
        value |= dilated_word(raw,d[3],d[2],d[1],yy,x,radius);
        if (value == 0xffffffffu) break;
    }
    int remaining = d[4] - word * 32;
    if (remaining < 32) value &= (1u << remaining) - 1u;
    padded[d[7] + row * d[6] + word] = value;
}

__kernel void count_runs(__global const uint *padded, __global const int *desc,
                         int count, __global int *row_counts) {
    int row = get_global_id(0), variant = get_global_id(1);
    if (variant >= count) return;
    __global const int *d = desc + variant * 16;
    if (row >= d[5]) return;
    uint carry = 0u;
    int total = 0;
    for (int word = 0; word < d[6]; ++word) {
        uint value = padded[d[7] + row * d[6] + word];
        total += popcount(value & ~((value << 1) | carry));
        carry = value >> 31;
    }
    row_counts[d[8] + row] = total;
}

__kernel void prefix_runs(__global int *desc, int count,
                          __global const int *row_counts,
                          __global int *prefix, __global int *total) {
    if (get_global_id(0)) return;
    int offset = 0;
    for (int variant = 0; variant < count; ++variant) {
        __global int *d = desc + variant * 16;
        d[9] = offset;
        for (int row = 0; row < d[5]; ++row) {
            prefix[d[8] + row] = offset;
            offset += row_counts[d[8] + row];
        }
        d[10] = offset - d[9];
    }
    *total = offset;
}

__kernel void emit_runs(__global const uint *padded, __global const int *desc,
                        int count, int gap, __global const int *prefix,
                        __global int4 *runs) {
    int row = get_global_id(0), variant = get_global_id(1);
    if (variant >= count) return;
    __global const int *d = desc + variant * 16;
    if (row >= d[5]) return;
    int at = prefix[d[8] + row], start = -1;
    for (int word = 0; word < d[6]; ++word) {
        uint value = padded[d[7] + row * d[6] + word];
        for (int bit = 0; bit < 32; ++bit) {
            int x = word * 32 + bit;
            if (x >= d[4]) break;
            int inside = (value >> bit) & 1u;
            if (inside && start < 0) start = x;
            else if (!inside && start >= 0) {
                runs[at++] = (int4)(row - gap, start - gap, x - 1 - gap, 0);
                start = -1;
            }
        }
    }
    if (start >= 0) runs[at] = (int4)(row - gap, start - gap, d[4] - 1 - gap, 0);
}

__kernel void locate_batch(__global const uint *tile, __global const int4 *runs,
                           __global const int *desc, int count, int stride,
                           int size, int margin, int origin_y, int step, __global uint *best) {
    int x = margin + get_global_id(0)*step, y = origin_y+get_global_id(1)*step, variant = get_global_id(2);
    if (variant >= count) return;
    __global const int *d = desc + variant * 16;
    if (x > d[13] || y > d[14]) return;
    uint key = (uint)((y * size + x) * 4 + d[12]);
    if (key >= atomic_add(best, 0u)) return;
    for (int i = d[9]; i < d[9] + d[10]; ++i) {
        int4 r = runs[i];
        int yy = y + r.x;
        if (yy < 0 || yy >= size) continue;
        int a = max(0, x + r.y), b = min(size - 1, x + r.z);
        if (a > b) continue;
        int left = a >> 5, right = b >> 5;
        uint lo = 0xffffffffu << (a & 31), hi = 0xffffffffu >> (31 - (b & 31));
        int base = yy * stride;
        if (left == right) {
            if (tile[base + left] & lo & hi) return;
        } else {
            if ((tile[base + left] & lo) || (tile[base + right] & hi)) return;
            for (int w = left + 1; w < right; ++w) if (tile[base + w]) return;
        }
    }
    atomic_min(best, key);
}

__kernel void commit_batch(__global uint *tile, __global const uint *raw,
                           __global const int *desc, int variant, int stride,
                           int x, int y) {
    int word = get_global_id(0), row = get_global_id(1);
    __global const int *d = desc + variant * 16;
    if (word >= d[2] || row >= d[1]) return;
    uint value = raw[d[3] + row * d[2] + word];
    int target = (y + row) * stride + (x >> 5) + word, shift = x & 31;
    atomic_or(tile + target, value << shift);
    if (shift && (x >> 5) + word + 1 < stride) atomic_or(tile + target + 1, value >> (32 - shift));
}

inline int fits(__global const uint *tile, __global const int4 *runs,
                 __global const int *d, int stride, int size, int margin, int x, int y) {
    if (x < margin || y < margin || x > d[13] || y > d[14]) return 0;
    for (int i = d[9]; i < d[9] + d[10]; ++i) {
        int4 r = runs[i]; int yy = y + r.x;
        if (yy < 0 || yy >= size) continue;
        int a = max(0,x + r.y), b = min(size - 1,x + r.z);
        if (a > b) continue;
        int first = a >> 5, last = b >> 5, base = yy * stride;
        uint lo = 0xffffffffu << (a & 31), hi = 0xffffffffu >> (31 - (b & 31));
        if (first == last) { if (tile[base + first] & lo & hi) return 0; }
        else {
            if ((tile[base + first] & lo) || (tile[base + last] & hi)) return 0;
            for (int w = first + 1; w < last; ++w) if (tile[base + w]) return 0;
        }
    }
    return 1;
}

__kernel void validate_fixed(__global const uint *tile, __global const int4 *runs,
                             __global const int *desc, int variant, int stride,
                             int size, int margin, int x, int y, __global uint *result) {
    if (!get_global_id(0)) *result = fits(tile,runs,desc + variant * 16,stride,size,margin,x,y);
}

__kernel void assemble_batch(__global const int *source, __global int *target,
                             int rotations, int slot, int raw_offset, int run_offset) {
    int index = get_global_id(0);
    if (index >= rotations * 16) return;
    int value = source[index], field = index & 15;
    if (field == 3) value += raw_offset;
    if (field == 9) value += run_offset;
    target[slot * 64 + index] = value;
}

__kernel void locate_multi(__global const uint *tiles,
    __global const int4 *runs, __global const int *descriptors,
    __global const int *active, int slots, int rotations, int stride, int size, int margin,
    __global const int *modes, __global const int *rotation_flags, __global const int *origin_y, int step, int score_origin, __global uint *best) {
    int slot = get_global_id(2) / 4, variant = get_global_id(2) & 3;
    if (slot >= slots || variant >= rotations || !active[slot]) return;
    // Local size is (32,4,1): every item in this group has the same slot.
    // One atomic bound read per group avoids millions of contended reads.
    __local uint bound;
    if (get_local_id(0) == 0 && get_local_id(1) == 0) bound = atomic_add(best + slot,0u);
    barrier(CLK_LOCAL_MEM_FENCE);
    __global const int *d = descriptors + slot * 64 + variant * 16;
    int x = margin + get_global_id(0)*step;
    // Bottom-left variants preserve horizontal long-strip rows. Minimum-top
    // variants may rotate strips vertically to fill residual pockets.
    if (!modes[slot] && !(rotation_flags[slot] & (1 << (d[11] & 1)))) return;
    int bias=modes[slot]*d[1];
    int y=origin_y[slot]+get_global_id(1)*step-score_origin*bias;
    if (x > d[13] || y < margin || y > d[14]) return;
    uint key = (uint)(((y+bias) * size + x) * 4 + d[12]);
    if (key >= bound) return;
    __global const uint *tile = tiles + slot * size * stride;
    for (int i = d[9]; i < d[9] + d[10]; ++i) {
        int4 r = runs[i]; int yy = y + r.x;
        if (yy < 0 || yy >= size) continue;
        int a = max(0,x + r.y), b = min(size - 1,x + r.z);
        if (a > b) continue;
        int first = a >> 5, last = b >> 5, base = yy * stride;
        uint lo = 0xffffffffu << (a & 31), hi = 0xffffffffu >> (31 - (b & 31));
        if (first == last) { if (tile[base + first] & lo & hi) return; }
        else {
            if ((tile[base + first] & lo) || (tile[base + last] & hi)) return;
            for (int w = first + 1; w < last; ++w) if (tile[base + w]) return;
        }
    }
    atomic_min(best + slot,key);
}

__kernel void commit_multi(__global uint *tiles,
    __global const uint *raw, __global const int *descriptors,
    __global const uint *best, __global const int *modes, int slots, int rotations, int stride, int size) {
    int slot = get_global_id(2), word = get_global_id(0), row = get_global_id(1);
    if (slot >= slots || best[slot] == 0xffffffffu) return;
    __global const int *desc = descriptors + slot * 64;
    int rank = best[slot] & 3, variant = 0;
    for (int v = 0; v < rotations; ++v) if (desc[v * 16 + 12] == rank) variant = v;
    __global const int *d = desc + variant * 16;
    if (word >= d[2] || row >= d[1]) return;
    uint position = best[slot] / 4;
    int x = position % size, y = position / size-modes[slot]*d[1], shift = x & 31;
    uint value = raw[d[3] + row * d[2] + word];
    int target = slot * size * stride + (y + row) * stride + (x >> 5) + word;
    atomic_or(tiles + target,value << shift);
    if (shift && (x >> 5) + word + 1 < stride) atomic_or(tiles + target + 1,value >> (32 - shift));
}
'''

# FP32 avoids the GPU's slow double-precision clipping on large selections.
# Expand row slabs and X bounds by 0.002 cells: the mask stays conservative
# after float conversion at all supported grids (up to 8192 cells).
_start=SOURCE.index('__kernel void raster_shell(')
_end=SOURCE.index('__kernel void rotate_shapes(')
_float=SOURCE[_start:_end].replace('raster_shell(', 'raster_shell_float(').replace('double','float')
_float=_float.replace('__global const float2 *triangles','__global const double2 *triangles').replace(
    'float scale, float grid','double scale, double grid').replace(
    '(triangles[(offset + t) * 3 + i] * scale) * grid;',
    'convert_float2((triangles[(offset + t) * 3 + i] * scale) * grid);')
_float=_float.replace('1e-10','0.002f').replace('float bound = row + side;',
    'float bound = row + side + (side ? 0.002f : -0.002f);')
SOURCE+=_float

# Four-layout batches bind cached shape buffers directly. This reuses the
# existing fit/commit bodies while avoiding copies and descriptor assembly.
_base=SOURCE[:SOURCE.index('__kernel void raster_shell_float(')]
_locate=_base[_base.index('__kernel void locate_multi('):_base.index('__kernel void commit_multi(')]
_locate=_locate.replace('locate_multi(', 'locate_multi_direct(').replace(
    '__global const int4 *runs, __global const int *descriptors,',
    '__global const int4 *r0, __global const int4 *r1, __global const int4 *r2, __global const int4 *r3, '
    '__global const int *d0, __global const int *d1, __global const int *d2, __global const int *d3,')
_locate=_locate.replace('__global const int *d = descriptors + slot * 64 + variant * 16;',
    '__global const int4 *runs=slot==0?r0:slot==1?r1:slot==2?r2:r3; '
    '__global const int *d=(slot==0?d0:slot==1?d1:slot==2?d2:d3)+variant*16;')
_commit=_base[_base.index('__kernel void commit_multi('):]
_commit=_commit.replace('commit_multi(', 'commit_multi_direct(').replace(
    '__global const uint *raw, __global const int *descriptors,',
    '__global const uint *r0, __global const uint *r1, __global const uint *r2, __global const uint *r3, '
    '__global const int *d0, __global const int *d1, __global const int *d2, __global const int *d3,')
_commit=_commit.replace('__global const int *desc = descriptors + slot * 64;',
    '__global const uint *raw=slot==0?r0:slot==1?r1:slot==2?r2:r3; '
    '__global const int *desc=slot==0?d0:slot==1?d1:slot==2?d2:d3;')
SOURCE+=_locate+_commit
