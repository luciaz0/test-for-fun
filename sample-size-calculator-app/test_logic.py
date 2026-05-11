from ab_calculator_logic import calculate_binary_sample_size, calculate_mean_sample_size

def test_binary():
    # p1=0.3, mde=0.05, groups=4, alpha=0.05, power=0.8
    res = calculate_binary_sample_size(0.3, 0.05, num_test_groups=4, alpha=0.05, power=0.8)
    print(f"Binary Test: {res}")
    assert res['sample_size_per_group'] > 0
    assert res['test_target_metric'] == 0.315 # 0.3 * 1.05

def test_mean():
    # mean=100, std=50, mde_abs=5, groups=1, alpha=0.05, power=0.8
    res = calculate_mean_sample_size(100, 50, 5, num_test_groups=1, alpha=0.05, power=0.8)
    print(f"Mean Test: {res}")
    assert res['sample_size_per_group'] > 0
    assert res['test_target_metric'] == 105.0

if __name__ == "__main__":
    test_binary()
    test_mean()
    print("All logic tests passed!")
