#pragma once

#include <algorithm>
#include <array>
#include <bit>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace fast32::experimental {

enum class paged_radix_shortcut : std::uint8_t { none, ascending, descending, dominant };

struct paged_radix_result {
	// Dominant partition results aggregate the passes of both child ranges;
	// copied_back means at least one child needed its final copy-back.
	unsigned radix_passes = 0;
	bool copied_back = false;
	paged_radix_shortcut shortcut = paged_radix_shortcut::none;
};

namespace paged_radix_detail {

inline constexpr unsigned digit_bits = 11;
inline constexpr unsigned digit_bins = 1U << digit_bits;
inline constexpr unsigned max_passes = (64 + digit_bits - 1) / digit_bits;
inline constexpr std::size_t interrupt_stride = 65536;
using histogram = std::array<std::uint32_t, digit_bins>;

struct digit {
	unsigned shift;
	unsigned mask;
};

// Optional page interface: contiguous(i) returns the live span starting at i,
// ending no later than that physical page's boundary. The operator[] fallback
// remains useful for independent implementations and correctness comparison.
template <class Values>
inline constexpr bool has_page_segments = requires(Values &values, std::size_t i) {
	values.contiguous(i);
};

template <class Values>
inline constexpr bool has_direct_data = requires(Values &values) {
	values.data();
	values.size();
};

template <class Values>
inline auto segment_at(Values &values, std::size_t index) {
	if constexpr (has_page_segments<Values>) {
		return values.contiguous(index);
	} else {
		return std::span(values.data() + index, values.size() - index);
	}
}

template <class Values>
struct page_slice {
	Values &values;
	std::size_t offset;
	std::size_t length;

	auto &operator[](std::size_t index) {
		return values[offset + index];
	}

	auto contiguous(std::size_t index) requires(has_page_segments<Values> || has_direct_data<Values>) {
		const auto segment = segment_at(values, offset + index);
		return segment.first(std::min(segment.size(), length - index));
	}
};

template <class Values>
struct page_cursor {
	using record_type = std::remove_reference_t<decltype(std::declval<Values &>()[0])>;
	Values &values;
	record_type *data = nullptr;
	std::size_t begin = 0;
	std::size_t end = 0;

	auto &at(std::size_t index) {
		if constexpr (has_page_segments<Values> || has_direct_data<Values>) {
			if (index < begin || index >= end) {
				const auto segment = segment_at(values, index);
				assert(!segment.empty());
				data = segment.data();
				begin = index;
				end = begin + segment.size();
			}
			return data[index - begin];
		} else {
			return values[index];
		}
	}
};

template <class Values, class Key>
inline bool sample_dominant(Values &values, std::size_t n, Key &key, std::uint64_t &candidate) {
	if (n < 256) {
		return false;
	}
	std::array<std::uint64_t, 32> sample {};
	for (unsigned i = 0; i < sample.size(); ++i) {
		const auto index = static_cast<std::size_t>((static_cast<std::uint64_t>(n - 1) * i) / (sample.size() - 1));
		if constexpr (has_page_segments<Values> || has_direct_data<Values>) {
			sample[i] = key(std::as_const(segment_at(values, index).front()));
		} else {
			sample[i] = key(std::as_const(values[index]));
		}
	}
	unsigned votes = 0;
	for (const auto value : sample) {
		if (votes == 0) {
			candidate = value;
			votes = 1;
		} else if (value == candidate) {
			++votes;
		} else {
			--votes;
		}
	}
	return std::count(sample.begin(), sample.end(), candidate) >= 28;
}

template <class Values, class Key, class Interrupt>
inline std::pair<std::size_t, std::size_t> partition_dominant(Values &values, std::size_t n, std::uint64_t pivot,
                                                              Key &key, Interrupt &interrupt) {
	page_cursor<Values> input {values};
	page_cursor<Values> lower {values};
	page_cursor<Values> upper {values};
	std::size_t left = 0;
	std::size_t scan = 0;
	std::size_t right = n;
	while (scan < right) {
		interrupt();
		// Every iteration consumes exactly one unclassified record, including
		// iterations that bring a new record back from the upper partition.
		const auto steps = std::min(interrupt_stride, right - scan);
		for (std::size_t step = 0; step < steps; ++step) {
			auto &record = input.at(scan);
			const auto value = key(std::as_const(record));
			if (value < pivot) {
				std::swap(record, lower.at(left));
				++left;
				++scan;
			} else if (value > pivot) {
				--right;
				std::swap(record, upper.at(right));
			} else {
				++scan;
			}
		}
	}
	interrupt();
	return {left, right};
}

template <class Interrupt, class Function>
inline void in_chunks(std::size_t n, Interrupt &interrupt, Function function) {
	for (std::size_t begin = 0; begin < n;) {
		interrupt();
		const auto end = begin + std::min(interrupt_stride, n - begin);
		function(begin, end);
		begin = end;
	}
}

template <class Values, class Interrupt, class Function>
inline void visit_chunks(Values &values, std::size_t n, Interrupt &interrupt, Function function) {
	in_chunks(n, interrupt, [&](std::size_t begin, std::size_t end) {
		if constexpr (has_page_segments<Values> || has_direct_data<Values>) {
			while (begin < end) {
				const auto segment = segment_at(values, begin);
				const auto count = std::min(segment.size(), end - begin);
				assert(count != 0);
				auto *data = segment.data();
				function(data, 0, count, begin);
				begin += count;
			}
		} else {
			function(values, begin, end, begin);
		}
	});
}

template <std::size_t... Passes>
inline void count_key(std::uint64_t value, std::array<histogram, max_passes> &counts,
                      const std::array<digit, max_passes> &digits, std::index_sequence<Passes...>) {
	(++counts[Passes][(value >> digits[Passes].shift) & digits[Passes].mask], ...);
}

template <unsigned Passes, class Pages, class Key, class Interrupt>
inline void count_histograms(Pages &values, std::size_t n, Key &key, Interrupt &interrupt,
                             std::array<histogram, max_passes> &counts, const std::array<digit, max_passes> &digits) {
	visit_chunks(values, n, interrupt, [&](auto &source, std::size_t begin, std::size_t end, std::size_t) {
		for (auto i = begin; i < end; ++i) {
			count_key(key(std::as_const(source[i])), counts, digits, std::make_index_sequence<Passes> {});
		}
	});
}

template <class Source, class Key, class Interrupt, class Position>
inline void scatter_to(Source &source, std::size_t n, Key &key, Interrupt &interrupt, const digit selected,
                       Position position) {
	visit_chunks(source, n, interrupt, [&](auto &input, std::size_t begin, std::size_t end, std::size_t) {
		auto i = begin;
		for (; end - i >= 4; i += 4) {
			const auto a = input[i];
			const auto b = input[i + 1];
			const auto c = input[i + 2];
			const auto d = input[i + 3];
			auto *pa = position((key(a) >> selected.shift) & selected.mask);
			auto *pb = position((key(b) >> selected.shift) & selected.mask);
			auto *pc = position((key(c) >> selected.shift) & selected.mask);
			auto *pd = position((key(d) >> selected.shift) & selected.mask);
			*pa = a;
			*pb = b;
			*pc = c;
			*pd = d;
		}
		for (; i < end; ++i) {
			const auto record = input[i];
			*position((key(record) >> selected.shift) & selected.mask) = record;
		}
	});
}

template <class Source, class Destination, class Key, class Interrupt>
inline void scatter(Source &source, Destination &destination, std::size_t n, Key &key, Interrupt &interrupt,
                    histogram &offsets, const digit selected) {
	if constexpr (has_page_segments<Destination>) {
		using record_type = std::remove_reference_t<decltype(destination[0])>;
		struct bucket_cursor {
			record_type *next = nullptr;
			std::uint32_t remaining = 0;
			std::uint32_t next_page_index = 0;
		};
		// At most 32 KiB for normal 64-bit-pointer targets, in addition to the
		// 49 KiB histograms. Each bucket advances sequentially through its output
		// pages, avoiding division/modulo and page lookup for every record.
		std::array<bucket_cursor, digit_bins> cursors {};
		const auto refill = [&](bucket_cursor &cursor) {
			assert(cursor.next_page_index < n);
			const auto segment = segment_at(destination, cursor.next_page_index);
			const auto count = std::min(segment.size(), n - cursor.next_page_index);
			assert(count != 0);
			cursor.next = segment.data();
			cursor.remaining = static_cast<std::uint32_t>(count);
			cursor.next_page_index += static_cast<std::uint32_t>(count);
		};
		for (unsigned bin = 0; bin <= selected.mask; ++bin) {
			const auto end = bin == selected.mask ? n : offsets[bin + 1];
			if (offsets[bin] < end) {
				cursors[bin].next_page_index = offsets[bin];
				refill(cursors[bin]);
			}
		}
		scatter_to(source, n, key, interrupt, selected, [&](unsigned bin) {
			auto &cursor = cursors[bin];
			if (cursor.remaining == 0) {
				refill(cursor);
			}
			--cursor.remaining;
			return cursor.next++;
		});
	} else {
		scatter_to(source, n, key, interrupt, selected, [&](unsigned bin) { return &destination[offsets[bin]++]; });
	}
}

// Sort complete records directly between caller-owned pages and one contiguous
// scratch buffer. No dynamic allocation occurs inside this function. The input
// and scratch storage must not overlap; scratch must hold at least n live Record
// objects, and n must fit uint32_t. Pages::operator[] must return Record&. An
// optional contiguous(i)->span<Record> enables direct sequential reads/writes
// and per-bucket output cursors that advance only when crossing page boundaries.
// Key must be a deterministic, nonmutating uint64_t extractor. Equal-key order is
// unspecified. Interrupt is called between bounded chunks and may throw; callers
// must discard a sort interrupted while writing its output pages.
template <bool AllowPartition, class Record, class Pages, class Key, class Interrupt>
inline paged_radix_result sort_impl(Pages &values, std::size_t n, std::span<Record> scratch, Key key,
                                    Interrupt interrupt) {
	static_assert(std::is_trivially_copyable_v<Record>);
	static_assert(std::is_same_v<decltype(values[std::size_t {}]), Record &>);
	static_assert(std::is_same_v<std::remove_cvref_t<decltype(key(std::declval<const Record &>()))>, std::uint64_t>);
	if (n > std::numeric_limits<std::uint32_t>::max()) {
		throw std::length_error("paged radix sort requires at most UINT32_MAX records");
	}
	if (scratch.size() < n) {
		throw std::invalid_argument("paged radix sort scratch is smaller than its input");
	}

	interrupt();
	if (n < 2) {
		return {0, false, paged_radix_shortcut::ascending};
	}

	const auto first = key(std::as_const(values[0]));
	auto previous = first;
	std::uint64_t varying = 0;
	bool ascending = true;
	bool descending = true;
	visit_chunks(values, n, interrupt, [&](auto &source, std::size_t begin, std::size_t end, std::size_t) {
		for (auto i = begin; i < end; ++i) {
			const auto current = key(std::as_const(source[i]));
			varying |= current ^ first;
			ascending &= previous <= current;
			descending &= previous >= current;
			previous = current;
		}
	});
	if (ascending) {
		return {0, false, paged_radix_shortcut::ascending};
	}
	if (descending) {
		in_chunks(n / 2, interrupt, [&](std::size_t begin, std::size_t end) {
			for (auto i = begin; i < end; ++i) {
				std::swap(values[i], values[n - 1 - i]);
			}
		});
		interrupt();
		return {0, false, paged_radix_shortcut::descending};
	}

	if constexpr (AllowPartition) {
		std::uint64_t pivot = 0;
		if (sample_dominant(values, n, key, pivot)) {
			// A misleading sample affects work, never correctness: classify
			// every record against the pivot and sort both remaining ranges.
			// Disable this shortcut for children to bound partition depth at
			// one, and reuse the same scratch prefix sequentially for each.
			const auto [left_end, right_begin] = partition_dominant(values, n, pivot, key, interrupt);
			page_slice<Pages> left {values, 0, left_end};
			page_slice<Pages> right {values, right_begin, n - right_begin};
			const auto left_result =
			    sort_impl<false, Record>(left, left.length, scratch.first(left.length), key, interrupt);
			const auto right_result =
			    sort_impl<false, Record>(right, right.length, scratch.first(right.length), key, interrupt);
			return {left_result.radix_passes + right_result.radix_passes,
			        left_result.copied_back || right_result.copied_back, paged_radix_shortcut::dominant};
		}
	}

	// Begin at the first varying bit, rather than at a fixed uint64_t boundary:
	// e.g. a 32-bit SQL key embedded in a 64-bit normalized key needs three
	// 11-bit passes regardless of its byte alignment. Skip constant digits.
	assert(varying != 0);
	std::array<digit, max_passes> digits {};
	unsigned pass_count = 0;
	const unsigned first_bit = std::countr_zero(varying); // typos:ignore
	const unsigned last_bit = 64U - std::countl_zero(varying);
	for (unsigned shift = first_bit; shift < last_bit; shift += digit_bits) {
		const unsigned bits = std::min(digit_bits, last_bit - shift);
		const unsigned mask = (1U << bits) - 1U;
		if (((varying >> shift) & mask) != 0) {
			digits[pass_count++] = {shift, mask};
		}
	}

	// All digit histograms are collected in one scan of the original pages.
	// Histograms remain valid through earlier stable LSD scatters.
	std::array<histogram, max_passes> counts {};
	switch (pass_count) {
	case 1:
		count_histograms<1>(values, n, key, interrupt, counts, digits);
		break;
	case 2:
		count_histograms<2>(values, n, key, interrupt, counts, digits);
		break;
	case 3:
		count_histograms<3>(values, n, key, interrupt, counts, digits);
		break;
	case 4:
		count_histograms<4>(values, n, key, interrupt, counts, digits);
		break;
	case 5:
		count_histograms<5>(values, n, key, interrupt, counts, digits);
		break;
	case 6:
		count_histograms<6>(values, n, key, interrupt, counts, digits);
		break;
	default:
		assert(false);
	}
	for (unsigned pass = 0; pass < pass_count; ++pass) {
		std::uint32_t offset = 0;
		for (unsigned bin = 0; bin <= digits[pass].mask; ++bin) {
			const auto count = counts[pass][bin];
			counts[pass][bin] = offset;
			offset += count;
		}
		assert(offset == n);
	}

	bool source_is_pages = true;
	for (unsigned pass = 0; pass < pass_count; ++pass) {
		if (source_is_pages) {
			scatter(values, scratch, n, key, interrupt, counts[pass], digits[pass]);
		} else {
			scatter(scratch, values, n, key, interrupt, counts[pass], digits[pass]);
		}
		source_is_pages = !source_is_pages;
	}
	if (!source_is_pages) {
		visit_chunks(values, n, interrupt,
		             [&](auto &output, std::size_t begin, std::size_t end, std::size_t global_begin) {
			             for (auto i = begin; i < end; ++i) {
				             output[i] = scratch[global_begin + i - begin];
			             }
		             });
	}
	interrupt();
	return {pass_count, !source_is_pages, paged_radix_shortcut::none};
}

} // namespace paged_radix_detail

template <class Record, class Pages, class Key, class Interrupt>
inline paged_radix_result paged_radix_sort(Pages &values, std::size_t n, std::span<Record> scratch, Key key,
                                           Interrupt interrupt) {
	return paged_radix_detail::sort_impl<true, Record>(values, n, scratch, key, interrupt);
}

} // namespace fast32::experimental
