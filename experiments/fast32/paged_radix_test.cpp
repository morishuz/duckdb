#include "duckdb/common/sorting/fast32_paged_radix.hpp"

#include <cstdlib>
#include <iostream>
#include <memory>
#include <random>
#include <string>
#include <vector>

namespace {

struct record {
	std::uint64_t key;
	std::uint64_t row_id;
	bool operator==(const record &) const = default;
};

template <class Record, bool Segments = false>
struct separate_pages {
	std::size_t count;
	std::size_t capacity;
	std::size_t indexed_reads = 0;
	std::size_t segment_lookups = 0;
	std::vector<std::unique_ptr<Record[]>> storage;

	separate_pages(std::size_t count_p, std::size_t capacity_p) : count(count_p), capacity(capacity_p) {
		for (std::size_t i = 0; i < count; i += capacity) {
			storage.push_back(std::make_unique<Record[]>(capacity));
		}
	}

	Record &operator[](std::size_t index) {
		++indexed_reads;
		return storage[index / capacity][index % capacity];
	}

	std::span<Record> contiguous(std::size_t index) requires(Segments) {
		++segment_lookups;
		assert(index < count);
		const auto offset = index % capacity;
		return {storage[index / capacity].get() + offset, std::min(capacity - offset, count - index)};
	}
};

std::size_t cases = 0;

void require(bool condition, const std::string &message) {
	if (!condition) {
		std::cerr << "FAILED: " << message << '\n';
		std::exit(1);
	}
}

std::uint64_t low_mask(unsigned bits) {
	return bits == 64 ? UINT64_MAX : (UINT64_C(1) << bits) - 1;
}

template <bool Segments>
fast32::experimental::paged_radix_result check_impl(const std::vector<std::uint64_t> &keys, std::size_t capacity,
                                                    const std::string &name) {
	separate_pages<record, Segments> pages(keys.size(), capacity);
	for (std::size_t i = 0; i < keys.size(); ++i) {
		pages[i] = {keys[i], i};
	}
	auto expected = keys;
	std::sort(expected.begin(), expected.end());
	std::vector<record> scratch(keys.size());
	std::size_t interrupts = 0;
	pages.indexed_reads = 0;
	const auto stats = fast32::experimental::paged_radix_sort<record>(
	    pages, keys.size(), scratch, [](const record &value) { return value.key; }, [&] { ++interrupts; });
	if constexpr (Segments) {
		if (stats.radix_passes != 0) {
			if (stats.shortcut != fast32::experimental::paged_radix_shortcut::dominant) {
				require(pages.indexed_reads == 1, name + " segment kernel indexed individual page records");
			}
			require(pages.segment_lookups > 0, name + " missing contiguous segment use");
		}
	}
	std::vector<bool> seen(keys.size());
	for (std::size_t i = 0; i < keys.size(); ++i) {
		const auto &value = pages[i];
		require(value.key == expected[i], name + " key mismatch");
		require(value.row_id < keys.size(), name + " invalid payload");
		require(!seen[value.row_id], name + " duplicate payload");
		require(value.key == keys[value.row_id], name + " detached payload");
		seen[value.row_id] = true;
	}
	require(interrupts > 0, name + " missing interrupt check");
	if (stats.shortcut != fast32::experimental::paged_radix_shortcut::dominant) {
		require(stats.copied_back == (stats.radix_passes % 2 == 1), name + " pass parity");
	}
	if (keys.size() > 65536) {
		require(interrupts >= 3, name + " missing bounded interrupt checks");
	}
	++cases;
	return stats;
}

fast32::experimental::paged_radix_result check(const std::vector<std::uint64_t> &keys, std::size_t capacity,
                                               const std::string &name) {
	const auto indexed = check_impl<false>(keys, capacity, name);
	const auto segmented = check_impl<true>(keys, capacity, name);
	require(indexed.radix_passes == segmented.radix_passes && indexed.copied_back == segmented.copied_back &&
	            indexed.shortcut == segmented.shortcut,
	        name + " accessor path disagrees");
	return segmented;
}

void boundaries_and_distributions(std::mt19937_64 &rng) {
	const std::size_t sizes[] = {0,    1,     2,     3,     17,    63,    64,    65,    257,
	                             4093, 16382, 16383, 16384, 32766, 32767, 32768, 65539, 131099};
	const std::size_t capacities[] = {7, 251, 16383, 32767};
	for (auto n : sizes) {
		for (auto capacity : capacities) {
			std::vector<std::uint64_t> keys(n);
			for (auto &key : keys) {
				key = rng();
			}
			check(keys, capacity, "random full64");
			for (auto &key : keys) {
				key = (rng() & UINT32_MAX) << 24;
			}
			check(keys, capacity, "normalized INTEGER");
			for (std::size_t i = 0; i < n; ++i) {
				keys[i] = (i % 101 == 0) ? rng() : 0;
			}
			check(keys, capacity, "99 percent zero");
			for (std::size_t i = 0; i < n; ++i) {
				keys[i] = (i * 97 % 13) * UINT64_C(0x123456781234567);
			}
			check(keys, capacity, "low cardinality full64");
		}
	}
}

void digit_ranges(std::mt19937_64 &rng) {
	for (unsigned width : {1U, 8U, 11U, 12U, 16U, 22U, 23U, 32U, 33U, 44U, 45U, 55U, 56U, 64U}) {
		for (unsigned shift : {0U, 1U, 7U, 8U, 16U, 24U, 31U, 32U}) {
			if (width + shift > 64) {
				continue;
			}
			const auto variable_mask = low_mask(width) << shift;
			const auto constant = UINT64_C(0xa5c3be97f048261d) & ~variable_mask;
			std::vector<std::uint64_t> keys(4093);
			for (auto &key : keys) {
				key = ((rng() << shift) & variable_mask) | constant;
			}
			const auto stats = check(keys, 251, "varying bit range");
			require(stats.radix_passes == (width + 10) / 11, "aligned pass count");
		}
	}
	std::vector<std::uint64_t> keys(32771);
	for (auto &key : keys) {
		key = rng() & ((UINT64_C(1) << 63) | 1);
	}
	require(check(keys, 16383, "constant middle digits").radix_passes == 2, "constant digit skipping");
	for (auto &key : keys) {
		key = rng() & ((UINT64_C(1) << 63) | (UINT64_C(1) << 22) | 1);
	}
	require(check(keys, 32767, "three separated digits").radix_passes == 3, "three sparse digits");
}

void ordered_inputs() {
	std::vector<std::uint64_t> keys(65539, UINT64_MAX);
	using shortcut = fast32::experimental::paged_radix_shortcut;
	require(check(keys, 16383, "equal").shortcut == shortcut::ascending, "equal shortcut");
	for (std::size_t i = 0; i < keys.size(); ++i) {
		keys[i] = i / 5;
	}
	require(check(keys, 32767, "sorted duplicates").shortcut == shortcut::ascending, "ascending shortcut");
	std::reverse(keys.begin(), keys.end());
	require(check(keys, 16383, "reverse duplicates").shortcut == shortcut::descending, "descending shortcut");
	keys[0] = 0;
	keys.back() = UINT64_MAX;
	require(check(keys, 251, "misleading endpoints").shortcut == shortcut::none, "fully verified order");
	std::fill(keys.begin(), keys.end(), 17);
	keys[65535] = 18;
	require(check(keys, 16383, "single late outlier").shortcut == shortcut::dominant, "fully verified equality");
}

void dominant_inputs(std::mt19937_64 &rng) {
	using shortcut = fast32::experimental::paged_radix_shortcut;
	std::vector<std::uint64_t> keys(150000);
	for (bool shifted : {false, true}) {
		const auto maximum = shifted ? UINT64_C(0xffffffff000000) : UINT64_MAX;
		const auto middle = shifted ? UINT64_C(0x80000000000000) : (UINT64_C(1) << 63);
		for (const auto pivot : {UINT64_C(0), middle, maximum}) {
			for (std::size_t i = 0; i < keys.size(); ++i) {
				keys[i] = i % 100 == 0 ? (shifted ? (rng() & UINT32_MAX) << 24 : rng()) : pivot;
			}
			require(check(keys, 16383, "dominant minimum/interior/maximum").shortcut == shortcut::dominant,
			        "dominant sample not selected");
		}
	}

	keys.resize(131099);
	for (bool misleading : {false, true}) {
		for (std::size_t i = 0; i < keys.size(); ++i) {
			keys[i] = !misleading && i % 3 == 0 ? (UINT64_C(1) << 63) : rng();
		}
		// Force a perfect sample for a pivot present in only about 1/3 of the
		// source, or in just these 32 records. Both remaining child ranges cross
		// multiple physical pages and still require complete 64-bit sorting.
		for (std::size_t i = 0; i < 32; ++i) {
			keys[(keys.size() - 1) * i / 31] = UINT64_C(1) << 63;
		}
		require(check(keys, 16383, "misleading dominant sample and large children").shortcut == shortcut::dominant,
		        "misleading sample case did not exercise partition");
	}

	keys.resize(70003);
	for (std::size_t i = 0; i < keys.size(); ++i) {
		keys[i] = i % 100 == 0 ? (rng() & 511) + ((i / 100) % 2 ? 1024 : 0) : 768;
	}
	const auto stats = check(keys, 32767, "two odd-pass dominant children");
	require(stats.shortcut == shortcut::dominant && stats.radix_passes == 2 && stats.copied_back,
	        "dominant child stats must aggregate passes and any copy-back");
}

void scalar_and_validation(std::mt19937_64 &rng) {
	separate_pages<std::uint64_t> pages(65539, 32767);
	std::vector<std::uint64_t> expected(65539);
	for (std::size_t i = 0; i < expected.size(); ++i) {
		pages[i] = expected[i] = rng();
	}
	std::vector<std::uint64_t> scratch(expected.size());
	auto key = [](const std::uint64_t &value) {
		return value;
	};
	fast32::experimental::paged_radix_sort<std::uint64_t>(pages, expected.size(), scratch, key, [] {});
	std::sort(expected.begin(), expected.end());
	for (std::size_t i = 0; i < expected.size(); ++i) {
		require(pages[i] == expected[i], "scalar sort");
	}
	++cases;

	bool invalid_scratch = false;
	try {
		fast32::experimental::paged_radix_sort<std::uint64_t>(pages, expected.size(), std::span(scratch).first(1), key,
		                                                      [] {});
	} catch (const std::invalid_argument &) {
		invalid_scratch = true;
	}
	require(invalid_scratch, "short scratch rejected");
	bool invalid_length = false;
	try {
		fast32::experimental::paged_radix_sort<std::uint64_t>(pages, static_cast<std::size_t>(UINT32_MAX) + 1, scratch,
		                                                      key, [] {});
	} catch (const std::length_error &) {
		invalid_length = true;
	}
	require(invalid_length, "oversized input rejected");
	for (std::size_t i = 0; i < expected.size(); ++i) {
		require(pages[i] == expected[i], "rejected input mutated");
	}
	++cases;

	struct interrupted {};
	bool stopped = false;
	std::size_t calls = 0;
	try {
		fast32::experimental::paged_radix_sort<std::uint64_t>(pages, expected.size(), scratch, key, [&] {
			if (++calls == 3) {
				throw interrupted {};
			}
		});
	} catch (const interrupted &) {
		stopped = true;
	}
	require(stopped && calls == 3, "interrupt propagated");
	++cases;
}

void cancellation_at_every_callback(std::mt19937_64 &rng) {
	constexpr std::size_t n = 150000;
	constexpr std::size_t capacity = 16383;
	for (bool dominant : {false, true}) {
		std::vector<std::uint64_t> keys(n);
		for (std::size_t i = 0; i < n; ++i) {
			keys[i] = dominant && i % 100 != 0 ? 0 : (rng() & UINT32_MAX) << 24;
		}
		const auto key = [](const record &value) {
			return value.key;
		};
		struct interrupted {};

		// First discover the complete callback count on this actual input. Three
		// active passes (in the child for dominant input) ensure both a scatter into
		// original pages and a final copy-back occur. The dominant case additionally
		// injects cancellation inside the in-place partition itself.
		separate_pages<record, true> baseline(n, capacity);
		for (std::size_t i = 0; i < n; ++i) {
			baseline[i] = {keys[i], i};
		}
		std::vector<record> baseline_scratch(n);
		std::size_t total_callbacks = 0;
		const auto stats = fast32::experimental::paged_radix_sort<record>(baseline, n, baseline_scratch, key,
		                                                                  [&] { ++total_callbacks; });
		require(stats.radix_passes == 3 && stats.copied_back,
		        "cancellation case must include page writes and copy-back");
		require((stats.shortcut == fast32::experimental::paged_radix_shortcut::dominant) == dominant,
		        "cancellation case took an unexpected path");
		require(total_callbacks > 3, "cancellation case must pass the initial read-only scan");

		for (std::size_t throw_at = 1; throw_at <= total_callbacks; ++throw_at) {
			separate_pages<record, true> pages(n, capacity);
			for (std::size_t i = 0; i < n; ++i) {
				pages[i] = {keys[i], i};
			}
			std::vector<record> scratch(n);
			std::size_t callbacks = 0;
			bool stopped = false;
			try {
				fast32::experimental::paged_radix_sort<record>(pages, n, scratch, key, [&] {
					if (++callbacks == throw_at) {
						throw interrupted {};
					}
				});
			} catch (const interrupted &) {
				stopped = true;
			}
			require(stopped && callbacks == throw_at, "cancellation did not propagate at the requested callback");
			++cases;

			// Failed output is deliberately discarded: cancellation during an
			// output scatter need not preserve a permutation. Start with entirely
			// fresh records and prove a later complete invocation remains correct.
			check_impl<true>(keys, capacity, "complete rerun after cancellation");
		}
	}
}

} // namespace

int main() {
	std::mt19937_64 rng(UINT64_C(0x8234a65197bcd0ef));
	boundaries_and_distributions(rng);
	digit_ranges(rng);
	ordered_inputs();
	dominant_inputs(rng);
	scalar_and_validation(rng);
	cancellation_at_every_callback(rng);
	std::cout << "paged_radix_test: " << cases << " cases passed\n";
}
