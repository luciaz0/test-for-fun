import numpy as np
from statsmodels.stats.power import NormalIndPower, TTestIndPower
from math import ceil

def calculate_binary_sample_size(p1, mde, num_test_groups=1, alpha=0.05, power=0.8):
    """
    Calculates sample size PER GROUP for binary metrics (e.g. Conversion Rate).
    
    p1: Baseline conversion rate (e.g., 0.10)
    mde: Relative lift expected (e.g., 0.05 for 5% lift)
    num_test_groups: Number of treatment arms (excluding control)
    """
    # Adjust alpha for multiple comparisons (Bonferroni)
    adj_alpha = alpha / num_test_groups
    
    p2 = p1 * (1 + mde)
    
    # Validation for proportion range
    if p2 > 1 or p2 < 0:
        return None
        
    analysis = NormalIndPower()
    
    # Cohen's h for proportions
    effect_size = 2 * (np.arcsin(np.sqrt(p2)) - np.arcsin(np.sqrt(p1)))
    
    sample_size_per_group = analysis.solve_power(effect_size=effect_size, 
                                                 alpha=adj_alpha, 
                                                 power=power, 
                                                 ratio=1.0)
    return {
        "sample_size_per_group": int(ceil(sample_size_per_group)),
        "control_metric": p1,
        "test_target_metric": round(p2, 4),
        "total_sample_size": int(ceil(sample_size_per_group)) * (num_test_groups + 1)
    }

def calculate_mean_sample_size(mean, std_dev, mde_absolute, num_test_groups=1, alpha=0.05, power=0.8):
    """
    Calculates sample size PER GROUP for mean-based metrics (e.g. GMV).
    
    mean: Baseline average
    std_dev: Standard deviation of the metric
    mde_absolute: The absolute change in mean you want to detect (e.g., $1.00)
    num_test_groups: Number of treatment arms (excluding control)
    """
    # Adjust alpha for multiple comparisons (Bonferroni)
    adj_alpha = alpha / num_test_groups
    target_mean = mean + mde_absolute
    
    # Cohen's d for means
    effect_size = mde_absolute / std_dev
    
    analysis = TTestIndPower()
    sample_size_per_group = analysis.solve_power(effect_size=effect_size, 
                                                 alpha=adj_alpha, 
                                                 power=power, 
                                                 ratio=1.0)
    return {
        "sample_size_per_group": int(ceil(sample_size_per_group)),
        "control_metric": mean,
        "test_target_metric": round(target_mean, 4),
        "total_sample_size": int(ceil(sample_size_per_group)) * (num_test_groups + 1)
    }
